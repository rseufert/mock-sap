"""OData V2 query handling and payload shaping, SAP Gateway style.

Implements the subset of OData V2 that SAP Gateway services actually expose
and that integration clients actually use: $filter, $select, $expand,
$orderby, $top, $skip, $inlinecount, $count, $format and the
``{"d": {"results": [...]}}`` envelope with ``__metadata`` / ``__deferred``.
"""
from __future__ import annotations

import datetime as _dt
import re
from typing import Any, Dict, List, Optional, Tuple

from .schema import EntityType, Prop, Service, set_for_type

EPOCH = _dt.datetime(1970, 1, 1)


class SapError(Exception):
    """An error that is rendered in the SAP Gateway error envelope."""

    def __init__(self, message, status=400, code="", target=""):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code or _default_code(status)
        self.target = target


def _default_code(status: int) -> str:
    return {
        400: "/IWBEP/CX_MGW_BUSI_EXCEPTION",
        401: "/IWBEP/CX_MGW_NOT_AUTH_EXCEPTION",
        403: "/IWBEP/CX_MGW_NOT_AUTH_EXCEPTION",
        404: "/IWBEP/CX_MGW_RESOURCE_NOT_FOUND",
        405: "/IWBEP/CX_MGW_NOT_IMPL_EXCEPTION",
        409: "/IWBEP/CX_MGW_BUSI_EXCEPTION",
        500: "/IWBEP/CX_MGW_TECH_EXCEPTION",
        501: "/IWBEP/CX_MGW_NOT_IMPL_EXCEPTION",
    }.get(status, "/IWBEP/CX_MGW_BUSI_EXCEPTION")


def error_payload(err: SapError, lang: str = "en") -> dict:
    """The JSON error body SAP Gateway returns."""
    return {
        "error": {
            "code": err.code,
            "message": {"lang": lang, "value": err.message},
            "innererror": {
                "application": {
                    "component_id": "",
                    "service_namespace": "/SAP/",
                    "service_id": "MOCK_SRV",
                    "service_version": "0001",
                },
                "transactionid": "MOCK0000000000000000000000000000",
                "timestamp": _dt.datetime.utcnow().strftime("%Y%m%d%H%M%S.%f")[:21],
                "Error_Resolution": {
                    "SAP_Transaction": "Run transaction /IWFND/ERROR_LOG on the mock gateway",
                    "SAP_Note": "See SAP Note 1797736 for error analysis",
                },
                "errordetails": [
                    {
                        "code": err.code,
                        "message": err.message,
                        "propertyref": err.target,
                        "severity": "error",
                        "target": err.target,
                    }
                ],
            },
        }
    }


# --------------------------------------------------------------------------
# Value conversion
# --------------------------------------------------------------------------

_DATE_RE = re.compile(r"^/Date\((-?\d+)([+-]\d+)?\)/$")


def to_json_value(prop: Prop, value: Any) -> Any:
    """Convert a stored value to its OData V2 JSON representation."""
    if value is None:
        return None
    t = prop.type
    if t == "Edm.Boolean":
        return bool(value)
    if t in ("Edm.Int32", "Edm.Int16", "Edm.Int64"):
        return int(value)
    if t == "Edm.Decimal":
        scale = prop.scale if prop.scale is not None else 3
        return ("%." + str(scale) + "f") % float(value)
    if t == "Edm.Double":
        return float(value)
    if t == "Edm.DateTime":
        dt = _parse_datetime(value)
        if dt is None:
            return None
        ms = int((dt - EPOCH).total_seconds() * 1000)
        return "/Date(%d)/" % ms
    if t == "Edm.Time":
        return str(value)
    return str(value)


def _parse_datetime(value: Any) -> Optional[_dt.datetime]:
    if isinstance(value, _dt.datetime):
        return value
    s = str(value)
    m = _DATE_RE.match(s)
    if m:
        return EPOCH + _dt.timedelta(milliseconds=int(m.group(1)))
    s = s.replace("Z", "").replace("T", " ").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return _dt.datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def to_db_value(prop: Prop, value: Any) -> Any:
    """Convert an inbound JSON value to its stored representation."""
    if value is None:
        return None
    t = prop.type
    try:
        if t == "Edm.Boolean":
            if isinstance(value, str):
                return 1 if value.lower() in ("true", "x", "1") else 0
            return 1 if value else 0
        if t in ("Edm.Int32", "Edm.Int16", "Edm.Int64"):
            return int(value)
        if t in ("Edm.Decimal", "Edm.Double"):
            return float(value)
        if t == "Edm.DateTime":
            dt = _parse_datetime(value)
            if dt is None:
                raise ValueError(value)
            return dt.isoformat()
    except (TypeError, ValueError):
        raise SapError(
            "Invalid value '%s' for property '%s' of type %s" % (value, prop.name, t),
            400, target=prop.name,
        )
    s = str(value)
    if prop.max_length and len(s) > prop.max_length:
        raise SapError(
            "Value of property '%s' exceeds maximum length %d" % (prop.name, prop.max_length),
            400, target=prop.name,
        )
    return s


# --------------------------------------------------------------------------
# $filter -> SQL
# --------------------------------------------------------------------------

_TOKEN_RE = re.compile(
    r"""\s*(?:
        (?P<dtlit>(?:datetimeoffset|datetime|guid|time)'(?:[^']|'')*')
      | (?P<str>'(?:[^']|'')*')
      | (?P<num>-?\d+\.\d+[mdfMDF]?|-?\d+[LlmMdDfF]?)
      | (?P<op>\(|\)|,|/)
      | (?P<ident>[A-Za-z_][A-Za-z0-9_]*)
    )""",
    re.VERBOSE,
)

_COMPARISON = {"eq": "=", "ne": "<>", "gt": ">", "ge": ">=", "lt": "<", "le": "<="}
_ARITH = {"add": "+", "sub": "-", "mul": "*", "div": "/", "mod": "%"}


class FilterError(SapError):
    def __init__(self, message):
        super().__init__(message, 400, code="/IWBEP/CX_MGW_BUSI_EXCEPTION")


def tokenize(expr: str) -> List[Tuple[str, str]]:
    tokens, pos = [], 0
    while pos < len(expr):
        if expr[pos].isspace():
            pos += 1
            continue
        m = _TOKEN_RE.match(expr, pos)
        if not m or m.end() == m.start():
            raise FilterError("Invalid token at position %d in $filter" % pos)
        pos = m.end()
        kind = m.lastgroup
        tokens.append((kind, m.group(kind)))
    return tokens


class _Frag:
    """A SQL fragment plus the bind values it consumes, in placeholder order."""

    __slots__ = ("sql", "params", "is_bool")

    def __init__(self, sql, params=None, is_bool=False):
        self.sql = sql
        self.params = list(params or [])
        self.is_bool = is_bool


class _Filter:
    """Recursive-descent parser turning $filter into a SQL WHERE fragment.

    Fragments carry their own bind values so that an argument used twice in
    the generated SQL (``endswith`` for instance) binds its value twice.
    """

    def __init__(self, tokens, et: EntityType):
        self.t = tokens
        self.i = 0
        self.et = et

    # -- helpers
    def peek(self):
        return self.t[self.i] if self.i < len(self.t) else None

    def next(self):
        tok = self.peek()
        if tok is None:
            raise FilterError("Unexpected end of $filter expression")
        self.i += 1
        return tok

    def accept_word(self, word):
        tok = self.peek()
        if tok and tok[0] == "ident" and tok[1].lower() == word:
            self.i += 1
            return True
        return False

    def expect(self, literal):
        tok = self.next()
        if tok[1] != literal:
            raise FilterError("Expected '%s' in $filter but found '%s'" % (literal, tok[1]))

    # -- grammar
    def parse(self):
        frag = self.parse_or()
        if self.peek():
            raise FilterError("Unexpected token '%s' in $filter" % self.peek()[1])
        return frag.sql, frag.params

    def parse_or(self):
        left = self.parse_and()
        while self.accept_word("or"):
            right = self.parse_and()
            left = _Frag("(%s OR %s)" % (left.sql, right.sql), left.params + right.params, True)
        return left

    def parse_and(self):
        left = self.parse_unary()
        while self.accept_word("and"):
            right = self.parse_unary()
            left = _Frag("(%s AND %s)" % (left.sql, right.sql), left.params + right.params, True)
        return left

    def parse_unary(self):
        if self.accept_word("not"):
            inner = self.parse_unary()
            return _Frag("(NOT %s)" % inner.sql, inner.params, True)
        return self.parse_comparison()

    def parse_comparison(self):
        left = self.parse_operand()
        tok = self.peek()
        if tok and tok[0] == "ident" and tok[1].lower() in _COMPARISON:
            op = _COMPARISON[self.next()[1].lower()]
            right = self.parse_operand()
            if right.sql == "NULL":
                return _Frag("%s IS %sNULL" % (left.sql, "" if op == "=" else "NOT "),
                             left.params, True)
            if left.sql == "NULL":
                return _Frag("%s IS %sNULL" % (right.sql, "" if op == "=" else "NOT "),
                             right.params, True)
            return _Frag("%s %s %s" % (left.sql, op, right.sql),
                         left.params + right.params, True)
        if left.is_bool:
            return left
        return _Frag("%s <> 0" % left.sql, left.params, True)

    def parse_operand(self):
        frag = self.parse_primary()
        while True:
            tok = self.peek()
            if tok and tok[0] == "ident" and tok[1].lower() in _ARITH:
                op = _ARITH[self.next()[1].lower()]
                rhs = self.parse_primary()
                frag = _Frag("(%s %s %s)" % (frag.sql, op, rhs.sql), frag.params + rhs.params)
            else:
                return frag

    def parse_primary(self):
        kind, text = self.next()
        if text == "(":
            inner = self.parse_or()
            self.expect(")")
            return _Frag("(%s)" % inner.sql, inner.params, inner.is_bool)
        if kind == "str":
            return _Frag("?", [text[1:-1].replace("''", "'")])
        if kind == "num":
            raw = re.sub(r"[LlmMdDfF]$", "", text)
            return _Frag("?", [float(raw) if "." in raw else int(raw)])
        if kind == "dtlit":
            value = text.split("'", 1)[1][:-1]
            dt = _parse_datetime(value)
            return _Frag("?", [dt.isoformat() if dt else value])
        if kind == "ident":
            low = text.lower()
            if low == "null":
                return _Frag("NULL")
            if low in ("true", "false"):
                return _Frag("?", [1 if low == "true" else 0])
            nxt = self.peek()
            if nxt and nxt[1] == "(":
                return self.parse_function(low)
            return self.column(text)
        raise FilterError("Unexpected token '%s' in $filter" % text)

    def column(self, name: str) -> "_Frag":
        # navigation paths (a/b) are not supported by the mock
        if self.peek() and self.peek()[1] == "/":
            raise FilterError("Filtering on navigation paths is not supported: %s" % name)
        if self.et.prop(name) is None:
            raise FilterError("Property '%s' not found in type '%s'" % (name, self.et.name))
        return _Frag('"%s"' % name)

    def parse_function(self, name):
        self.expect("(")
        args = []
        if self.peek() and self.peek()[1] != ")":
            while True:
                args.append(self.parse_operand())
                if self.peek() and self.peek()[1] == ",":
                    self.next()
                    continue
                break
        self.expect(")")
        n = len(args)

        def need(count):
            if n != count:
                raise FilterError("Function %s expects %d argument(s)" % (name, count))

        def combine(template, order, is_bool=False):
            """`order` lists arg indexes in the order they appear in `template`."""
            params = []
            for idx in order:
                params.extend(args[idx].params)
            return _Frag(template % tuple(args[i].sql for i in order), params, is_bool)

        if name == "substringof":
            need(2)
            return combine("instr(%s, %s) > 0", [1, 0], True)
        if name == "contains":  # V4 spelling, accepted for convenience
            need(2)
            return combine("instr(%s, %s) > 0", [0, 1], True)
        if name == "startswith":
            need(2)
            return combine("instr(%s, %s) = 1", [0, 1], True)
        if name == "endswith":
            need(2)
            return combine("substr(%s, -length(%s)) = %s", [0, 1, 1], True)
        if name == "indexof":
            need(2)
            return combine("(instr(%s, %s) - 1)", [0, 1])
        if name == "tolower":
            need(1)
            return combine("lower(%s)", [0])
        if name == "toupper":
            need(1)
            return combine("upper(%s)", [0])
        if name == "trim":
            need(1)
            return combine("trim(%s)", [0])
        if name == "length":
            need(1)
            return combine("length(%s)", [0])
        if name == "concat":
            need(2)
            return combine("(%s || %s)", [0, 1])
        if name == "substring":
            if n == 2:
                return combine("substr(%s, %s + 1)", [0, 1])
            need(3)
            return combine("substr(%s, %s + 1, %s)", [0, 1, 2])
        if name in ("year", "month", "day", "hour", "minute", "second"):
            need(1)
            fmt = {"year": "%%Y", "month": "%%m", "day": "%%d",
                   "hour": "%%H", "minute": "%%M", "second": "%%S"}[name]
            return combine("CAST(strftime('" + fmt + "', %s) AS INTEGER)", [0])
        raise FilterError("Unsupported $filter function '%s'" % name)


def build_where(expr: str, et: EntityType) -> Tuple[str, List[Any]]:
    return _Filter(tokenize(expr), et).parse()


def build_orderby(expr: str, et: EntityType) -> str:
    parts = []
    for chunk in expr.split(","):
        bits = chunk.strip().split()
        if not bits:
            continue
        name = bits[0]
        if et.prop(name) is None:
            raise SapError("Property '%s' not found in type '%s'" % (name, et.name), 400)
        direction = "ASC"
        if len(bits) > 1:
            if bits[1].lower() not in ("asc", "desc"):
                raise SapError("Invalid $orderby direction '%s'" % bits[1], 400)
            direction = bits[1].upper()
        parts.append('"%s" %s' % (name, direction))
    if not parts:
        raise SapError("Empty $orderby", 400)
    return ", ".join(parts)


# --------------------------------------------------------------------------
# Key predicates
# --------------------------------------------------------------------------

_KEY_PAIR_RE = re.compile(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)")


def parse_key_predicate(text: str, et: EntityType) -> Dict[str, Any]:
    """Parse ``('1000000')`` or ``(SalesOrder='4711',SalesOrderItem='000010')``."""
    inner = text.strip()
    if inner.startswith("(") and inner.endswith(")"):
        inner = inner[1:-1]
    parts = _split_top_level(inner)
    keys = et.keys
    values: Dict[str, Any] = {}
    if len(parts) == 1 and "=" not in parts[0]:
        if len(keys) != 1:
            raise SapError(
                "Type '%s' has %d key properties, provide them as name=value pairs"
                % (et.name, len(keys)), 400)
        values[keys[0].name] = _literal(parts[0], keys[0])
        return values
    for part in parts:
        m = _KEY_PAIR_RE.match(part)
        if not m:
            raise SapError("Malformed key predicate: %s" % text, 400)
        name, raw = m.group(1), m.group(2).strip()
        prop = et.prop(name)
        if prop is None or not prop.key:
            raise SapError("'%s' is not a key property of '%s'" % (name, et.name), 400)
        values[name] = _literal(raw, prop)
    missing = [k.name for k in keys if k.name not in values]
    if missing:
        raise SapError("Missing key properties: %s" % ", ".join(missing), 400)
    return values


def _split_top_level(text: str) -> List[str]:
    parts, buf, in_str = [], "", False
    for ch in text:
        if ch == "'":
            in_str = not in_str
        if ch == "," and not in_str:
            parts.append(buf)
            buf = ""
        else:
            buf += ch
    if buf.strip():
        parts.append(buf)
    return [p.strip() for p in parts if p.strip()]


def _literal(raw: str, prop: Prop) -> Any:
    raw = raw.strip()
    if raw.startswith("'") and raw.endswith("'"):
        return raw[1:-1].replace("''", "'")
    for prefix in ("datetime", "guid", "time"):
        if raw.lower().startswith(prefix + "'"):
            return to_db_value(prop, raw.split("'", 1)[1][:-1])
    return to_db_value(prop, re.sub(r"[LlmMdDfF]$", "", raw))


def row_get(row, name, default=None):
    """Read a column from either a sqlite3.Row or a plain dict."""
    try:
        value = row[name]
    except (IndexError, KeyError):
        return default
    return default if value is None else value


def key_predicate(et: EntityType, row) -> str:
    keys = et.keys

    def fmt(p):
        v = row_get(row, p.name)
        if p.type in ("Edm.Int32", "Edm.Int16", "Edm.Int64"):
            return str(int(v)) if v is not None else "0"
        if p.type == "Edm.DateTime":
            return "datetime'%s'" % to_json_value(p, v)
        return "'%s'" % str(v).replace("'", "''")
    if len(keys) == 1:
        return "(%s)" % fmt(keys[0])
    return "(%s)" % ",".join("%s=%s" % (p.name, fmt(p)) for p in keys)


# --------------------------------------------------------------------------
# Serialization
# --------------------------------------------------------------------------

def entity_uri(base_url: str, svc: Service, et: EntityType, row) -> str:
    return "%s%s/%s%s" % (base_url, svc.path, set_for_type(svc, et.name), key_predicate(et, row))


def serialize_entity(row, et: EntityType, svc: Service, base_url: str,
                     select: Optional[List[str]] = None,
                     expand: Optional[Dict[str, Any]] = None) -> dict:
    """Render one row in SAP's OData V2 JSON shape."""
    uri = entity_uri(base_url, svc, et, row)
    out: Dict[str, Any] = {
        "__metadata": {
            "id": uri,
            "uri": uri,
            "type": "%s.%s" % (svc.namespace, et.edm_name),
        }
    }
    for p in et.props:
        if select and p.name not in select:
            continue
        out[p.name] = to_json_value(p, row_get(row, p.name))
    expand = expand or {}
    for nav in et.navs:
        if select and nav.name not in select and nav.name not in expand:
            continue
        if nav.name in expand:
            out[nav.name] = expand[nav.name]
        else:
            out[nav.name] = {"__deferred": {"uri": "%s/%s" % (uri, nav.name)}}
    return out


def collection_envelope(entities: List[dict], count: Optional[int] = None) -> dict:
    d: Dict[str, Any] = {"results": entities}
    if count is not None:
        d["__count"] = str(count)
    return {"d": d}


def entity_envelope(entity: dict) -> dict:
    return {"d": entity}
