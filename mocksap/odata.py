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
from urllib.parse import quote

from . import clock, money
from .schema import (COMPLEX_TYPES, ENTITY_TYPES, EntityType, Prop, Service,
                     set_for_type)

EPOCH = _dt.datetime(1970, 1, 1)


class SapError(Exception):
    """An error that is rendered in the SAP Gateway error envelope."""

    def __init__(self, message, status=400, code="", target=""):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code or _default_code(status)
        self.target = target


def whole_number(value, name: str) -> int:
    """A whole number that is not negative, or a 400 naming what was sent."""
    number = -1
    if not isinstance(value, bool):
        try:
            number = int(value)
            if isinstance(value, float) and value != number:
                number = -1
        except (TypeError, ValueError):
            pass
    if number < 0:
        raise SapError("%s is a whole number, not %r" % (name, value), 400)
    return number


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
                "timestamp": clock.now().strftime("%Y%m%d%H%M%S.%f")[:21],
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
        # V2 serves a decimal as a string, at its declared scale, and it is
        # read out of the column as one: no float stands between the stored
        # figure and the wire.
        return money.text(value, money.scale_of(prop))
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
        if t == "Edm.Decimal":
            # Quantised on the way in, so the column holds the scale the
            # property declares rather than whatever the client happened to
            # send - and rounded half up, the way SAP rounds.  An empty
            # string is not a zero: a client that sent one meant something,
            # and Gateway tells it so rather than guessing.
            if isinstance(value, str) and not value.strip():
                raise ValueError(value)
            return money.text(value, money.scale_of(prop))
        if t == "Edm.Double":
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
# What a client may write
# --------------------------------------------------------------------------


def check_writable(et: EntityType, payload: Any, creating: bool) -> None:
    """Refuse a payload that writes a property `$metadata` declares read-only.

    `Prop.creatable` and `Prop.updatable` are what `metadata.py` renders as
    `sap:creatable="false"` and `sap:updatable="false"`, so the check and the
    advertisement come from the same declaration and cannot drift. Shipping
    `$metadata` so a client can generate its model from it, and then not
    enforcing what it says, tells that client a constraint it will discover is
    real only in production (#97).

    This lives on the OData surface rather than in `store`, because the
    annotation is this surface's promise. The application behind it writes the
    fields it owns - `apply_delivery_status` sets `OverallDeliveryStatus`,
    which is read-only to a client precisely because the application decides
    it - and those writes go through `store` without passing here.

    A key declared non-creatable is a key the server will assign, not one a
    client may not send: SAP lets a caller number its own sales order item,
    and a deep insert here does the same. So keys are left to `store`, which
    assigns the ones that arrive empty and refuses a change to one that does
    not.
    """
    if not isinstance(payload, dict):
        return
    for name, value in payload.items():
        if name in ("__metadata", "__count", "__deferred"):
            continue
        nav = et.nav(name)
        if nav is not None:
            children = value
            if isinstance(children, dict) and "results" in children:
                children = children["results"]
            if isinstance(children, dict):
                children = [children]
            if isinstance(children, list):
                for child in children:
                    check_writable(ENTITY_TYPES[nav.target], child, creating)
            continue
        prop = et.prop(name)
        if prop is None:
            continue            # `store.split_payload` names it better
        if prop.complex_type:
            if not isinstance(value, dict):
                continue        # `store._flatten_complex` says so better
            ct = COMPLEX_TYPES[prop.complex_type]
            for sub_name in value:
                sub = ct.prop(sub_name)
                if sub is not None:
                    _refuse_read_only(sub, creating, "%s/%s" % (name, sub_name))
            continue
        if prop.key:
            continue
        _refuse_read_only(prop, creating, name)


def _refuse_read_only(prop: Prop, creating: bool, target: str) -> None:
    allowed, facet = ((prop.creatable, "sap:creatable")
                      if creating else (prop.updatable, "sap:updatable"))
    if allowed:
        return
    raise SapError(
        "Property '%s' is read-only: $metadata declares it %s=\"false\""
        % (target, facet), 400, target=target)


# --------------------------------------------------------------------------
# $filter -> SQL
# --------------------------------------------------------------------------

_TOKEN_RE = re.compile(
    r"""\s*(?:
        (?P<dtlit>(?:datetimeoffset|datetime|guid|time)'(?:[^']|'')*')
      | (?P<str>'(?:[^']|'')*')
      | (?P<v4date>\d{4}-\d{2}-\d{2}
            (?:T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})?)?)
      | (?P<num>-?\d+\.\d+[mdfMDF]?|-?\d+[LlmMdDfF]?)
      | (?P<op>\(|\)|,|/)
      | (?P<ident>[A-Za-z_][A-Za-z0-9_]*)
    )""",
    re.VERBOSE,
)

_COMPARISON = {"eq": "=", "ne": "<>", "gt": ">", "ge": ">=", "lt": "<", "le": "<="}
_ARITH = {"add": "+", "sub": "-", "mul": "*", "div": "/", "mod": "%"}


def column_sql(column: str, prop: Prop) -> str:
    """How a column is named in SQL that compares, sorts or sums it.

    A decimal is stored as text so that its scale survives, and SQLite would
    then compare it as text: `'99.000' > '100.000'` is true of the strings
    and false of the amounts.  So a decimal is cast where it is used as a
    number.  SQLite has no decimal type to cast to, so the comparison itself
    goes through a double - the stored figure stays exact, and a filter
    boundary is the one place in this mock where an amount is a float.
    """
    if prop.type == "Edm.Decimal":
        return 'CAST("%s" AS REAL)' % column
    return '"%s"' % column


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

    def __init__(self, tokens, et: EntityType, version: int = 2):
        self.t = tokens
        self.i = 0
        self.et = et
        self.version = version

    def wrong_dialect(self, text: str, instead: str) -> "FilterError":
        """A literal spelled for the other OData version, and how to spell it."""
        return FilterError(
            "%s is an OData V%d literal, and this service speaks V%d; "
            "write %s" % (text, 2 if self.version >= 4 else 4, self.version,
                          instead))

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
            if raw != text and self.version >= 4:
                raise self.wrong_dialect(text, raw)
            return _Frag("?", [float(raw) if "." in raw else int(raw)])
        if kind == "dtlit":
            value = text.split("'", 1)[1][:-1]
            if self.version >= 4:
                raise self.wrong_dialect(text, _v4_spelling(text, value))
            dt = _parse_datetime(value)
            return _Frag("?", [dt.isoformat() if dt else value])
        if kind == "v4date":
            if self.version < 4:
                raise self.wrong_dialect(text, "datetime'%s'" % _v2_moment(text))
            moment = _v4_moment(text)
            if moment is None:
                raise FilterError("'%s' is not a date in $filter" % text)
            return _Frag("?", [moment.isoformat()])
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
        path = name
        if self.peek() and self.peek()[1] == "/":
            self.next()
            tail = self.next()
            if tail[0] != "ident":
                raise FilterError("Expected a property name after '%s/'" % name)
            path = "%s/%s" % (name, tail[1])
        resolved = self.et.resolve(path)
        if resolved is None:
            if self.et.prop(name) is not None and "/" not in path:
                raise FilterError(
                    "'%s' is a structured property; filter on one of its "
                    "sub-properties, as in %s/..." % (name, name))
            if "/" in path and self.et.prop(name) is not None:
                raise FilterError(
                    "Property '%s' not found in the structured property '%s'"
                    % (path.split("/")[1], name))
            raise FilterError(
                "Property '%s' not found in type '%s'" % (path, self.et.name))
        return _Frag(column_sql(resolved[0], resolved[1]))

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


def _v4_moment(text: str) -> Optional[_dt.datetime]:
    """A V4 date or date-time literal as the naive UTC moment a row stores."""
    try:
        if "T" not in text:
            return _dt.datetime.strptime(text, "%Y-%m-%d")
        stamp, offset = text, _dt.timedelta()
        if stamp.endswith("Z"):
            stamp = stamp[:-1]
        elif stamp[-6] in "+-" and stamp[-3] == ":":
            hours, minutes = int(stamp[-5:-3]), int(stamp[-2:])
            offset = _dt.timedelta(hours=hours, minutes=minutes)
            if stamp[-6] == "-":
                offset = -offset
            stamp = stamp[:-6]
        for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
            try:
                # +02:00 is two hours ahead of UTC, so the UTC moment is earlier
                return _dt.datetime.strptime(stamp, fmt) - offset
            except ValueError:
                continue
    except ValueError:
        pass
    return None


def _v2_moment(text: str) -> str:
    """How a V4 date literal would have been written inside datetime'...'."""
    moment = _v4_moment(text)
    return moment.isoformat() if moment else text


def _v4_spelling(text: str, value: str) -> str:
    """How a V2 prefixed literal is written in V4, for the refusal to quote."""
    kind = text.split("'", 1)[0].lower()
    if kind in ("datetime", "datetimeoffset"):
        moment = _parse_datetime(value)
        if moment is not None:
            return moment.isoformat() + "Z"
    return value


def build_where(expr: str, et: EntityType, version: int = 2) -> Tuple[str, List[Any]]:
    """`$filter` as SQL, read in the dialect of the service it was sent to.

    The two versions spell a literal differently - `datetime'2026-01-01T00:00:00'`
    and `100.50M` in V2, `2026-01-01T00:00:00Z` and `100.50` in V4 - and a
    service takes its own. Both used to be read as V2 everywhere, so a V4
    service accepted the literals of the version it does not speak and refused
    its own (#165).
    """
    return _Filter(tokenize(expr), et, version).parse()


def build_orderby(expr: str, et: EntityType) -> str:
    parts = []
    for chunk in expr.split(","):
        bits = chunk.strip().split()
        if not bits:
            continue
        name = bits[0]
        resolved = et.resolve(name)
        if resolved is None:
            raise SapError("Property '%s' cannot be sorted on in type '%s'"
                           % (name, et.name), 400)
        name = resolved[0]
        direction = "ASC"
        if len(bits) > 1:
            if bits[1].lower() not in ("asc", "desc"):
                raise SapError("Invalid $orderby direction '%s'" % bits[1], 400)
            direction = bits[1].upper()
        parts.append("%s %s" % (column_sql(name, resolved[1]), direction))
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

def _selected(select, name):
    """What of `name` was asked for: None when nothing, [] for all of it, or
    the list of sub-properties named by paths like `Address/City`."""
    if select is None:
        return []
    subs = []
    for entry in select:
        if entry == name:
            return []
        if entry.startswith(name + "/"):
            subs.append(entry.split("/", 1)[1])
    return subs or None


def _complex_value(prop: Prop, row, svc: Service, subs) -> dict:
    """Render a complex property back into the nested shape SAP sends."""
    ct = COMPLEX_TYPES[prop.complex_type]
    value = {"__metadata": {"type": "%s.%s" % (svc.namespace, ct.name)}}
    for sub in ct.props:
        if subs and sub.name not in subs:
            continue
        value[sub.name] = to_json_value(sub, row_get(row, "%s_%s" % (prop.name, sub.name)))
    return value


def etag_for(et: EntityType, row) -> Optional[str]:
    """The weak ETag of a row, or None if the type is not concurrency-controlled.

    SAP renders the ETag from the properties marked ConcurrencyMode="Fixed" -
    for a timestamp that is a percent-escaped OData datetime literal, which is
    what a Gateway service puts in `__metadata.etag` and the ETag header.
    """
    props = et.concurrency_props
    if not props:
        return None
    parts = []
    for prop in props:
        value = row_get(row, prop.name)
        if prop.type == "Edm.DateTime":
            dt = _parse_datetime(value)
            literal = dt.isoformat() if dt else ""
            parts.append("datetime'%s'" % quote(literal, safe=""))
        else:
            parts.append(quote(str(value if value is not None else ""), safe=""))
    return 'W/"%s"' % ",".join(parts)


def etag_matches(header: str, etag: Optional[str]) -> bool:
    """Compare an If-Match / If-None-Match header against an entity's ETag.

    `*` matches anything that exists, and the weak-validator prefix is ignored,
    as RFC 7232 allows for the weak comparison these headers use.
    """
    if header is None:
        return False
    candidates = [c.strip() for c in header.split(",") if c.strip()]
    if "*" in candidates:
        return etag is not None
    if etag is None:
        return False
    def normalise(value):
        value = value.strip()
        if value.startswith("W/"):
            value = value[2:]
        return value.strip('"')
    current = normalise(etag)
    return any(normalise(c) == current for c in candidates)


def entity_uri(base_url: str, svc: Service, et: EntityType, row) -> str:
    return "%s%s/%s%s" % (base_url, svc.path, set_for_type(svc, et.name), key_predicate(et, row))


def serialize_entity(row, et: EntityType, svc: Service, base_url: str,
                     select: Optional[List[str]] = None,
                     expand: Optional[Dict[str, Any]] = None) -> dict:
    """Render one row in SAP's OData V2 JSON shape."""
    uri = entity_uri(base_url, svc, et, row)
    meta: Dict[str, Any] = {
        "id": uri,
        "uri": uri,
        "type": "%s.%s" % (svc.namespace, et.edm_name),
    }
    etag = etag_for(et, row)
    if etag is not None:
        meta["etag"] = etag
    out: Dict[str, Any] = {"__metadata": meta}
    for p in et.props:
        chosen = _selected(select, p.name)
        if select is not None and chosen is None:
            continue
        if p.complex_type:
            out[p.name] = _complex_value(p, row, svc, chosen)
        else:
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
