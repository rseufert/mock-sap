"""The OData V2 request dispatcher.

`dispatch` is deliberately transport agnostic: the HTTP server calls it, and
so does the $batch handler for every sub-request inside a multipart batch.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qsl, unquote, urlparse

from . import apply as odata_apply
from . import messages as sap_messages, metadata, metadata4, odata4, store
from .odata import (SapError, build_orderby, build_where, collection_envelope,
                    entity_envelope, entity_uri, error_payload, etag_for,
                    etag_matches, key_predicate, parse_key_predicate,
                    serialize_entity, to_json_value)
from .schema import ENTITY_TYPES, SERVICES, EntityType, Service, service_for_path

JSON_CT = "application/json;charset=utf-8"
XML_CT = "application/xml;charset=utf-8"

_SHARED_OPTIONS = {
    "$filter", "$select", "$expand", "$orderby", "$top", "$skip", "$format",
    "$search", "$skiptoken", "sap-client", "sap-language", "saml2", "$callback",
}
# $count is a path segment in V2 (/A_SalesOrder/$count), not a query option
SUPPORTED_OPTIONS = _SHARED_OPTIONS | {"$inlinecount", "$links"}
SUPPORTED_OPTIONS_V4 = _SHARED_OPTIONS | {"$count", "$ref", "$apply"}

_SEGMENT_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)(\(.*\))?$")


class Response:
    def __init__(self, status=200, headers=None, body=b"", content_type=JSON_CT):
        self.status = status
        self.headers = dict(headers or {})
        if content_type and "Content-Type" not in self.headers:
            self.headers["Content-Type"] = content_type
        if isinstance(body, (dict, list)):
            body = json.dumps(body, indent=2, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.body = body

    @classmethod
    def error(cls, err: SapError, fmt: str = "json", version: int = 2):
        if version >= 4:
            return cls(err.status, body=odata4.error_payload(err))
        if fmt == "xml":
            from xml.sax.saxutils import escape
            body = (
                '<?xml version="1.0" encoding="utf-8"?>'
                '<error xmlns="http://schemas.microsoft.com/ado/2007/08/dataservices/metadata">'
                "<code>%s</code><message xml:lang=\"en\">%s</message></error>"
                % (escape(err.code), escape(err.message))
            )
            return cls(err.status, body=body, content_type=XML_CT)
        return cls(err.status, body=error_payload(err))


class Context:
    """Everything a request handler needs that is not part of the URL."""

    def __init__(self, conn, base_url, user="MOCKUSER", client="100",
                 require_if_match=False):
        self.conn = conn
        self.base_url = base_url
        self.user = user
        self.client = client
        # Newer Gateway services refuse to modify a concurrency-controlled
        # entity that arrives without a validator; classic ones do not.
        self.require_if_match = require_if_match


# --------------------------------------------------------------------------


def parse_query(query: str) -> Dict[str, str]:
    out = {}
    for key, value in parse_qsl(query, keep_blank_values=True):
        out[key] = value
    return out


def _wants_xml(opts: Dict[str, str], headers: Dict[str, str]) -> bool:
    fmt = (opts.get("$format") or "").lower()
    if fmt == "json":
        return False
    if fmt in ("xml", "atom"):
        return True
    accept = (headers.get("accept") or "").lower()
    return "application/json" not in accept and ("xml" in accept or "atom" in accept)


def _check_options(opts: Dict[str, str], version: int = 2) -> None:
    allowed = SUPPORTED_OPTIONS_V4 if version >= 4 else SUPPORTED_OPTIONS
    for key in opts:
        if not key.startswith("$"):
            continue
        if key not in allowed:
            if key in SUPPORTED_OPTIONS or key in SUPPORTED_OPTIONS_V4:
                raise SapError(
                    "Query option '%s' belongs to OData V%d, and this service "
                    "speaks V%d" % (key, 2 if version >= 4 else 4, version), 400)
            raise SapError("Query option '%s' is not supported" % key, 400)


def _split_options(text: str, separator: str) -> List[str]:
    """Split on `separator` at the top level, ignoring nested parentheses."""
    parts, buf, depth, in_str = [], "", 0, False
    for ch in text:
        if ch == "'":
            in_str = not in_str
        elif not in_str and ch == "(":
            depth += 1
        elif not in_str and ch == ")":
            depth -= 1
        if ch == separator and depth == 0 and not in_str:
            parts.append(buf)
            buf = ""
        else:
            buf += ch
    if buf.strip():
        parts.append(buf)
    return [p.strip() for p in parts if p.strip()]


def _parse_expand(expr: str, et: EntityType, version: int = 2) -> Dict[str, dict]:
    """Parse $expand into a tree of {"options": …, "expand": …} nodes.

    V2 expands by path (`to_Item/to_SalesOrder`); V4 nests options inside
    parentheses (`to_Item($select=Material;$top=5)`), which real V4 clients
    lean on heavily.
    """
    tree: Dict[str, dict] = {}
    for item in _split_options(expr, ","):
        options: Dict[str, str] = {}
        if item.endswith(")") and "(" in item:
            path, _, raw = item.partition("(")
            if version < 4:
                raise SapError(
                    "Options inside $expand are OData V4 syntax, and this "
                    "service speaks V2", 400, target=path.strip())
            for option in _split_options(raw[:-1], ";"):
                key, _, value = option.partition("=")
                key = key.strip()
                if key not in ("$select", "$filter", "$orderby", "$top", "$skip", "$count"):
                    raise SapError(
                        "Option '%s' is not supported inside $expand" % key, 400)
                options[key] = value.strip()
            item = path.strip()

        node, current_type = tree, et
        segments = item.split("/")
        for index, segment in enumerate(segments):
            nav = current_type.nav(segment)
            if nav is None:
                raise SapError(
                    "Navigation property '%s' not found in type '%s'"
                    % (segment, current_type.name), 400, target=segment)
            node = node.setdefault(segment, {"options": {}, "expand": {}})
            if index == len(segments) - 1:
                node["options"].update(options)
            current_type = ENTITY_TYPES[nav.target]
            node = node["expand"]
    return tree


def _int_option(opts, name) -> Optional[int]:
    if name not in opts:
        return None
    try:
        value = int(opts[name])
    except ValueError:
        raise SapError("Invalid value for %s: '%s'" % (name, opts[name]), 400)
    if value < 0:
        raise SapError("%s must not be negative" % name, 400)
    return value


def _render(ctx: Context, row, et: EntityType, svc: Service,
            select: Optional[List[str]], expand: Dict[str, dict]) -> dict:
    v4 = svc.version >= 4
    expanded: Dict[str, Any] = {}
    for nav_name, node in expand.items():
        nav = et.nav(nav_name)
        target = ENTITY_TYPES[nav.target]
        target_svc = _service_of(target.name, svc)
        options, subtree = node["options"], node["expand"]
        if nav.multiplicity == "*":
            where = " AND ".join('"%s" = ?' % remote for _l, remote in nav.join)
            params = [row[local] for local, _r in nav.join]
            if options.get("$filter"):
                extra, extra_params = build_where(options["$filter"], target)
                where, params = _merge_where(where, params, extra, extra_params)
            order = build_orderby(options["$orderby"], target) if options.get("$orderby") else ""
            kids = store.query(ctx.conn, target, where, params, order,
                               _int_option(options, "$top"), _int_option(options, "$skip"))
            child_select = _select_list(options, target) if options.get("$select") else None
            rendered = [_render(ctx, k, target, target_svc, child_select, subtree)
                        for k in kids]
            if (options.get("$count") or "").lower() == "true":
                expanded[nav_name + "@odata.count"] = store.count(
                    ctx.conn, target, where, params)
            # V4 expands to a bare array; V2 wraps it in a results object
            expanded[nav_name] = rendered if v4 else {"results": rendered}
        else:
            keys = {remote: row[local] for local, remote in nav.join}
            parent = store.get(ctx.conn, target, keys)
            child_select = _select_list(options, target) if options.get("$select") else None
            expanded[nav_name] = (
                _render(ctx, parent, target, target_svc, child_select, subtree)
                if parent else None)
    if v4:
        return odata4.serialize_entity(row, et, svc, ctx.base_url, select, expanded,
                                       etag=etag_for(et, row))
    return serialize_entity(row, et, svc, ctx.base_url, select, expanded)


def _service_of(type_name: str, fallback: Service) -> Service:
    """Which service should describe `type_name` in this response.

    The service handling the request wins, and after that only a service of
    the same dialect: an entity type now lives in a V2 and a V4 service at
    once, and answering a V4 request with V2 URLs - or V2 shapes - for an
    expanded entity would be worse than answering with an awkward URL.
    """
    if type_name in fallback.sets.values():
        return fallback
    candidates = [svc for svc in SERVICES.values() if type_name in svc.sets.values()]
    for svc in candidates:
        if svc.version == fallback.version:
            return svc
    return fallback


def _context_for(ctx: Context, svc: Service, set_name: str, opts,
                 single: bool = False) -> str:
    """The @odata.context a V4 response carries."""
    fragment = set_name
    select = (opts.get("$select") or "").strip()
    if select and select != "*":
        fragment += "(%s)" % ",".join(part.strip() for part in select.split(","))
    if single:
        fragment += "/$entity"
    return odata4.context_url(ctx.base_url, svc, fragment)


def _select_list(opts, et: EntityType) -> Optional[List[str]]:
    if "$select" not in opts or opts["$select"].strip() in ("", "*"):
        return None
    names = []
    for raw in opts["$select"].split(","):
        path = raw.strip()
        if not path:
            continue
        head = path.split("/")[0]
        prop = et.prop(head)
        if prop is None and et.nav(head) is None:
            raise SapError("Property '%s' not found in type '%s'" % (head, et.name),
                           400, target=head)
        # a path into a structured property selects part of it
        if "/" in path and prop is not None and prop.complex_type:
            if et.resolve(path) is None:
                raise SapError(
                    "Property '%s' not found in the structured property '%s'"
                    % (path.split("/", 1)[1], head), 400, target=path)
            names.append(path)
            continue
        names.append(head)
    return names


# --------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------


def dispatch(ctx: Context, method: str, path: str, query: str,
             headers: Dict[str, str], body: bytes) -> Response:
    svc, rest = service_for_path(path)
    if svc is None:
        raise SapError("Resource not found for the segment '%s'" % path.strip("/"), 404)
    opts = parse_query(query)
    xml = _wants_xml(opts, headers) and svc.version < 4
    try:
        return _dispatch(ctx, svc, rest, method.upper(), opts, headers, body, xml)
    except SapError as err:
        return Response.error(err, "xml" if xml else "json", svc.version)


def _dispatch(ctx, svc, rest, method, opts, headers, body, xml) -> Response:
    rest = rest.strip("/")

    if rest in ("", "/"):
        if method != "GET":
            raise SapError("Method %s is not allowed on the service document" % method, 405)
        if svc.version >= 4:
            return Response(body=odata4.service_document(svc, ctx.base_url))
        if xml:
            return Response(body=metadata.service_document_xml(svc, ctx.base_url),
                            content_type="application/atomsvc+xml;charset=utf-8")
        return Response(body=metadata.service_document_json(svc, ctx.base_url))

    if rest == "$metadata":
        if method != "GET":
            raise SapError("Method %s is not allowed on $metadata" % method, 405)
        if svc.version >= 4:
            # V4 metadata negotiates: CSDL JSON when JSON is asked for
            accept = (headers.get("accept") or "").lower()
            if (opts.get("$format") or "").lower() == "json" or (
                    "json" in accept and "xml" not in accept):
                return Response(body=metadata4.metadata_json(svc),
                                headers={"OData-Version": "4.0"})
            return Response(body=metadata4.metadata_document(svc), content_type=XML_CT,
                            headers={"OData-Version": "4.0"})
        return Response(body=metadata.metadata_document(svc), content_type=XML_CT,
                        headers={"DataServiceVersion": "2.0"})

    _check_options(opts, svc.version)
    segments = _split_path(rest)
    first = segments[0]
    m = _SEGMENT_RE.match(first)
    if not m:
        raise SapError("Resource not found for the segment '%s'" % first, 404)
    set_name, predicate = m.group(1), m.group(2)
    if set_name not in svc.sets:
        raise SapError("Resource not found for the segment '%s'" % set_name, 404)
    et = ENTITY_TYPES[svc.sets[set_name]]

    tail = segments[1:]

    # ---- collection level ------------------------------------------------
    if predicate is None:
        if tail == ["$count"]:
            if method != "GET":
                raise SapError("Method %s is not allowed on $count" % method, 405)
            if opts.get("$apply"):
                raise SapError(
                    "The $count segment cannot be combined with $apply; ask for "
                    "$count=true beside it instead", 400)
            where, params = _where_from(opts, et)
            return Response(body=str(store.count(ctx.conn, et, where, params)),
                            content_type="text/plain;charset=utf-8")
        if tail:
            raise SapError("Resource not found for the segment '%s'" % tail[0], 404)
        if method == "GET":
            return _read_collection(ctx, svc, et, set_name, opts)
        if method == "POST":
            return _create(ctx, svc, et, set_name, opts, body)
        raise SapError("Method %s is not allowed on entity set '%s'" % (method, set_name), 405)

    keys = parse_key_predicate(predicate, et)

    # ---- entity level ----------------------------------------------------
    if not tail:
        if method == "GET":
            row = store.get(ctx.conn, et, keys)
            if row is None:
                raise SapError(_not_found(et, keys), 404)
            etag = etag_for(et, row)
            if etag and etag_matches(headers.get("if-none-match"), etag):
                return Response(304, headers={"ETag": etag}, body=b"", content_type=None)
            select = _select_list(opts, et)
            expand = _parse_expand(opts.get("$expand", ""), et, svc.version) if opts.get("$expand") else {}
            entity = _render(ctx, row, et, svc, select, expand)
            if svc.version >= 4:
                body = odata4.entity_envelope(
                    entity, _context_for(ctx, svc, set_name, opts, single=True))
                response = Response(body=body, headers={"OData-Version": "4.0"})
            else:
                response = Response(body=entity_envelope(entity))
            if etag:
                response.headers["ETag"] = etag
            return response
        if method in ("PATCH", "MERGE", "PUT"):
            _check_if_match(ctx, et, keys, headers)
            payload = _json_body(body)
            store.update(ctx.conn, et, keys, payload, merge=(method != "PUT"), user=ctx.user)
            warnings = sap_messages.after_write(ctx.conn, et, keys)
            etag = etag_for(et, store.get(ctx.conn, et, keys))
            response = Response(204, headers={"ETag": etag} if etag else {},
                                body=b"", content_type=None)
            return _with_messages(response, warnings)
        if method == "DELETE":
            _check_if_match(ctx, et, keys, headers)
            store.delete(ctx.conn, et, keys)
            return Response(204, body=b"", content_type=None)
        raise SapError("Method %s is not allowed on an entity" % method, 405)

    row = store.get(ctx.conn, et, keys)
    if row is None:
        raise SapError(_not_found(et, keys), 404)

    # ---- association links ($links in V2, $ref in V4) ---------------------
    if tail[0] == "$links" and svc.version < 4:
        return _links(ctx, svc, et, row, tail[1:], method, opts, body)
    if svc.version >= 4 and tail[-1] == "$ref" and len(tail) >= 2:
        return _links(ctx, svc, et, row, tail[:-1], method, opts, body)

    # ---- property value --------------------------------------------------
    if len(tail) >= 1 and et.prop(tail[0]) is not None:
        prop = et.prop(tail[0])
        if method != "GET":
            raise SapError("Method %s is not allowed on a property" % method, 405)
        value = to_json_value(prop, row[prop.name])
        if len(tail) == 2 and tail[1] == "$value":
            return Response(body="" if value is None else str(value),
                            content_type="text/plain;charset=utf-8")
        if len(tail) == 1:
            return Response(body={"d": {prop.name: value}})
        raise SapError("Resource not found for the segment '%s'" % tail[1], 404)

    # ---- navigation ------------------------------------------------------
    nav_segment = tail[0]
    nm = _SEGMENT_RE.match(nav_segment)
    nav = et.nav(nm.group(1)) if nm else None
    if nav is None:
        raise SapError("Resource not found for the segment '%s'" % nav_segment, 404)
    target = ENTITY_TYPES[nav.target]
    target_svc = _service_of(target.name, svc)
    target_set = _set_name(target_svc, target.name)

    join_where = " AND ".join('"%s" = ?' % remote for _l, remote in nav.join)
    join_params = [row[local] for local, _r in nav.join]

    if nm.group(2) or nav.multiplicity == "1":
        if nm.group(2):
            child_keys = parse_key_predicate(nm.group(2), target)
        else:
            child_keys = {remote: row[local] for local, remote in nav.join}
        child = store.get(ctx.conn, target, child_keys)
        if child is None:
            raise SapError(_not_found(target, child_keys), 404)
        if method == "GET":
            select = _select_list(opts, target)
            expand = _parse_expand(opts.get("$expand", ""), target, target_svc.version) if opts.get("$expand") else {}
            return Response(body=entity_envelope(
                _render(ctx, child, target, target_svc, select, expand)))
        raise SapError("Method %s is not allowed on this resource" % method, 405)

    if tail[1:] == ["$count"]:
        where, params = _where_from(opts, target)
        combined, all_params = _merge_where(join_where, join_params, where, params)
        return Response(body=str(store.count(ctx.conn, target, combined, all_params)),
                        content_type="text/plain;charset=utf-8")
    if tail[1:]:
        raise SapError("Resource not found for the segment '%s'" % tail[1], 404)

    if method == "GET":
        return _read_collection(ctx, target_svc, target, target_set, opts,
                                extra_where=join_where, extra_params=join_params)
    if method == "POST":
        parent_keys = {remote: row[local] for local, remote in nav.join}
        return _create(ctx, target_svc, target, target_set, opts, body, parent_keys)
    raise SapError("Method %s is not allowed on '%s'" % (method, nav_segment), 405)


def _not_found(et: EntityType, keys) -> str:
    return "No entity with key %s found in '%s'" % (
        ", ".join("%s='%s'" % (k, v) for k, v in keys.items()), et.name)


def _set_name(svc: Service, type_name: str) -> str:
    for set_name, tname in svc.sets.items():
        if tname == type_name:
            return set_name
    return type_name


def _split_path(rest: str) -> List[str]:
    """Split on '/' but keep key predicates intact."""
    out, buf, depth = [], "", 0
    for ch in rest:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "/" and depth == 0:
            out.append(buf)
            buf = ""
        else:
            buf += ch
    if buf:
        out.append(buf)
    return [unquote(s) for s in out if s]


def _where_from(opts, et) -> Tuple[str, list]:
    if "$filter" not in opts or not opts["$filter"].strip():
        return "", []
    return build_where(opts["$filter"], et)


def _merge_where(w1, p1, w2, p2):
    if w1 and w2:
        return "(%s) AND (%s)" % (w1, w2), list(p1) + list(p2)
    return (w1 or w2), list(p1) + list(p2)


def _read_collection(ctx, svc, et, set_name, opts, extra_where="", extra_params=()):
    if opts.get("$apply"):
        return _read_aggregate(ctx, svc, et, set_name, opts, extra_where, extra_params)
    where, params = _where_from(opts, et)
    where, params = _merge_where(extra_where, extra_params, where, params)
    order = build_orderby(opts["$orderby"], et) if opts.get("$orderby") else ""
    top = _int_option(opts, "$top")
    skip = _int_option(opts, "$skip")
    select = _select_list(opts, et)
    expand = _parse_expand(opts.get("$expand", ""), et, svc.version) if opts.get("$expand") else {}

    rows = store.query(ctx.conn, et, where, params, order, top, skip)
    total = _wants_count(opts, svc)
    if total:
        total = store.count(ctx.conn, et, where, params)
    else:
        total = None

    entities = [_render(ctx, r, et, svc, select, expand) for r in rows]
    if svc.version >= 4:
        return Response(
            body=odata4.collection_envelope(
                entities, _context_for(ctx, svc, set_name, opts), total),
            headers={"OData-Version": "4.0"})
    return Response(body=collection_envelope(entities, total),
                    headers={"DataServiceVersion": "2.0"})


def _read_aggregate(ctx, svc, et, set_name, opts, extra_where, extra_params) -> Response:
    """Answer an $apply pipeline: rows of grouping keys and aggregates.

    These are not entities, so they carry no ETag, no id and no navigation -
    only what the pipeline produced, and a context naming those columns.
    """
    for option in ("$select", "$expand", "$filter"):
        if opts.get(option):
            raise SapError(
                "%s cannot be combined with $apply; put it inside the pipeline, "
                "as in $apply=filter(...)/groupby(...)" % option, 400)

    plan = odata_apply.parse(opts["$apply"], et)
    if extra_where:
        plan.where = ("(%s) AND (%s)" % (extra_where, plan.where)
                      if plan.where else extra_where)
        plan.params = list(extra_params) + list(plan.params)
    if opts.get("$orderby"):
        odata_apply._orderby(plan, opts["$orderby"])
    top, skip = _int_option(opts, "$top"), _int_option(opts, "$skip")
    if top is not None:
        plan.top = top
    if skip is not None:
        plan.skip = skip

    rows = odata_apply.run(ctx.conn, et, plan)

    total = None
    if _wants_count(opts, svc):
        counting = odata_apply.parse(opts["$apply"], et)
        counting.where, counting.params = plan.where, plan.params
        # how many groups there are, not how many this page shows
        counting.top = counting.skip = None
        counting.order = ""
        total = len(odata_apply.run(ctx.conn, et, counting))

    context = odata4.context_url(
        ctx.base_url, svc, "%s(%s)" % (set_name, ",".join(plan.names)))
    return Response(body=odata4.collection_envelope(rows, context, total),
                    headers={"OData-Version": "4.0"})


def _wants_count(opts, svc) -> bool:
    """V4 asks with $count=true, V2 with $inlinecount=allpages."""
    if svc.version >= 4:
        raw = (opts.get("$count") or "").lower()
        if raw in ("", "false"):
            return False
        if raw == "true":
            return True
        raise SapError("Invalid value '%s' for $count" % opts["$count"], 400)
    inline = (opts.get("$inlinecount") or "").lower()
    if inline == "allpages":
        return True
    if inline not in ("", "none"):
        raise SapError("Invalid value '%s' for $inlinecount" % opts["$inlinecount"], 400)
    return False


def _check_if_match(ctx: Context, et: EntityType, keys: Dict[str, Any],
                    headers: Dict[str, str]) -> None:
    """Enforce optimistic concurrency before a modifying request.

    Only types that declare a concurrency property have an ETag; for anything
    else a validator is meaningless and is ignored, which is how Gateway
    behaves for entity sets without ConcurrencyMode="Fixed".
    """
    row = store.get(ctx.conn, et, keys)
    if row is None:
        raise SapError(_not_found(et, keys), 404)
    etag = etag_for(et, row)
    if etag is None:
        return
    supplied = headers.get("if-match")
    if supplied is None:
        if ctx.require_if_match:
            raise SapError(
                "This request must carry an If-Match header with the ETag of "
                "'%s'. Read the entity first and send back its ETag." % et.name,
                428, code="/IWBEP/CX_MGW_BUSI_EXCEPTION")
        return
    if not etag_matches(supplied, etag):
        raise SapError(
            "The entity was changed by another user after it was read. Read it "
            "again and repeat the change (current ETag %s)." % etag,
            412, code="/IWBEP/CX_MGW_BUSI_EXCEPTION")


def _links(ctx, svc, et: EntityType, row, tail: List[str], method: str,
           opts: Dict[str, str], body: bytes) -> Response:
    """Serve `…/$links/<nav>`, the addressable form of an association.

    Reads answer with bare URIs instead of entities.  Writes re-point the
    foreign key columns named in `Nav.join`; where those columns are part of
    the dependent's key - which is the case for every composition, a sales
    order and its items for instance - repointing them would be a key change,
    and SAP rejects that.  `store.update` already draws exactly that line, so
    the write paths go through it rather than reimplementing the rule.
    """
    if not tail:
        raise SapError("The $links segment must be followed by a navigation property", 400)

    match = _SEGMENT_RE.match(tail[0])
    nav = et.nav(match.group(1)) if match else None
    if nav is None:
        raise SapError("Resource not found for the segment '%s'" % tail[0], 404)
    target = ENTITY_TYPES[nav.target]
    target_svc = _service_of(target.name, svc)
    predicate = match.group(2)
    rest = tail[1:]

    join_where = " AND ".join('"%s" = ?' % remote for _l, remote in nav.join)
    join_params = [row[local] for local, _r in nav.join]

    if rest == ["$count"]:
        if method != "GET":
            raise SapError("Method %s is not allowed on $count" % method, 405)
        where, params = _where_from(opts, target)
        combined, all_params = _merge_where(join_where, join_params, where, params)
        return Response(body=str(store.count(ctx.conn, target, combined, all_params)),
                        content_type="text/plain;charset=utf-8")
    if rest:
        raise SapError("Resource not found for the segment '%s'" % rest[0], 404)

    if method == "GET":
        if predicate:
            raise SapError("A key predicate is not allowed when reading $links", 400)
        return _read_links(ctx, target_svc, target, nav, row, opts,
                           join_where, join_params)

    if method in ("POST", "PUT", "MERGE", "PATCH"):
        if nav.multiplicity == "*" and method != "POST":
            raise SapError(
                "Use POST to add a link to the collection '%s'" % nav.name, 405)
        if nav.multiplicity == "1" and method == "POST":
            raise SapError(
                "Use PUT to set the link '%s', which refers to a single entity" % nav.name,
                405)
        payload = _json_body(body)
        other = _resolve_link_uri(
            ctx, target_svc, target,
            payload.get("@odata.id") if svc.version >= 4 else payload.get("uri"))
        _write_link(ctx, et, row, nav, target, other)
        return Response(204, body=b"", content_type=None)

    if method == "DELETE":
        other = None
        if predicate:
            other_keys = parse_key_predicate(predicate, target)
            other = store.get(ctx.conn, target, other_keys)
            if other is None:
                raise SapError(_not_found(target, other_keys), 404)
        elif nav.multiplicity == "*":
            raise SapError(
                "Deleting a link in the collection '%s' needs the key of the entity "
                "to unlink, as in $links/%s(...)" % (nav.name, nav.name), 400)
        _write_link(ctx, et, row, nav, target, other, clear=True)
        return Response(204, body=b"", content_type=None)

    raise SapError("Method %s is not allowed on $links" % method, 405)


def _read_links(ctx, target_svc, target: EntityType, nav, row, opts,
                join_where, join_params) -> Response:
    v4 = target_svc.version >= 4
    if nav.multiplicity == "1":
        keys = {remote: row[local] for local, remote in nav.join}
        other = store.get(ctx.conn, target, keys)
        if other is None:
            raise SapError(_not_found(target, keys), 404)
        uri = entity_uri(ctx.base_url, target_svc, target, other)
        if v4:
            return Response(body={
                "@odata.context": odata4.context_url(
                    ctx.base_url, target_svc, "$ref"),
                "@odata.id": uri}, headers={"OData-Version": "4.0"})
        return Response(body={"d": {"uri": uri}})

    where, params = _where_from(opts, target)
    where, params = _merge_where(join_where, join_params, where, params)
    order = build_orderby(opts["$orderby"], target) if opts.get("$orderby") else ""
    rows = store.query(ctx.conn, target, where, params, order,
                       _int_option(opts, "$top"), _int_option(opts, "$skip"))
    key = "@odata.id" if v4 else "uri"
    links = [{key: entity_uri(ctx.base_url, target_svc, target, r)} for r in rows]

    total = store.count(ctx.conn, target, where, params) \
        if _wants_count(opts, target_svc) else None
    if v4:
        return Response(
            body=odata4.collection_envelope(
                links, odata4.context_url(ctx.base_url, target_svc, "$ref"), total),
            headers={"OData-Version": "4.0"})
    return Response(body=collection_envelope(links, total),
                    headers={"DataServiceVersion": "2.0"})


def _resolve_link_uri(ctx, target_svc, target: EntityType, uri):
    """Turn the `{"uri": …}` payload of a link write into the row it names."""
    if not uri or not isinstance(uri, str):
        raise SapError(
            "The request body must be a JSON object holding the URI of the "
            'entity to link, as in {"uri": "…"} (V2) or {"@odata.id": "…"} (V4)',
            400, target="uri")
    path = urlparse(uri).path or uri
    segment = unquote(path.rstrip("/").rsplit("/", 1)[-1])
    match = _SEGMENT_RE.match(segment)
    if not match or not match.group(2):
        raise SapError("'%s' does not address an entity" % uri, 400, target="uri")
    set_name = match.group(1)
    expected = _set_name(target_svc, target.name)
    if set_name != expected:
        raise SapError(
            "The link must refer to an entity of '%s', but '%s' was given"
            % (expected, set_name), 400, target="uri")
    keys = parse_key_predicate(match.group(2), target)
    other = store.get(ctx.conn, target, keys)
    if other is None:
        raise SapError(_not_found(target, keys), 404)
    return other


def _write_link(ctx, et: EntityType, row, nav, target: EntityType, other,
                clear: bool = False) -> None:
    """Point a link at `other`, or clear it.

    For a to-many navigation the dependent carries the foreign key, so the
    *other* row is updated; for a to-one navigation this row carries it.
    """
    if nav.multiplicity == "*":
        subject, subject_type = other, target
        values = {remote: (None if clear else row[local]) for local, remote in nav.join}
    else:
        subject, subject_type = row, et
        values = {local: (None if clear else other[remote]) for local, remote in nav.join}
    for name, value in list(values.items()):
        if value is None:
            values[name] = store.initial_value(subject_type.prop(name))
    keys = {p.name: subject[p.name] for p in subject_type.keys}
    store.update(ctx.conn, subject_type, keys, values, merge=True, user=ctx.user)


def _json_body(body: bytes) -> dict:
    if not body:
        raise SapError("A request body is required", 400)
    try:
        payload = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise SapError("The request body is not valid JSON: %s" % exc, 400)
    if isinstance(payload, dict) and "d" in payload and isinstance(payload["d"], dict):
        payload = payload["d"]
    if not isinstance(payload, dict):
        raise SapError("The request body must be a JSON object", 400)
    return payload


def _with_messages(response: Response, warnings: List[dict]) -> Response:
    """Attach warnings to a response without changing its status."""
    header = sap_messages.to_header(warnings)
    if header:
        response.headers["sap-message"] = header
    return response


def _create(ctx, svc, et, set_name, opts, body, parent_keys=None) -> Response:
    payload = _json_body(body)
    keys = store.insert(ctx.conn, et, payload, user=ctx.user, parent_keys=parent_keys)
    warnings = list(sap_messages.after_write(ctx.conn, et, keys))
    for nav in et.navs:
        if nav.multiplicity != "*" or nav.name not in payload:
            continue
        child = ENTITY_TYPES[nav.target]
        parent = store.get(ctx.conn, et, keys)
        for kid in store.children(ctx.conn, parent, et, nav):
            warnings.extend(sap_messages.after_write(
                ctx.conn, child, {p.name: kid[p.name] for p in child.keys}))
    row = store.get(ctx.conn, et, keys)
    expand = _parse_expand(opts.get("$expand", ""), et, svc.version) if opts.get("$expand") else {}
    entity = _render(ctx, row, et, svc, None, expand)
    location = entity_uri(ctx.base_url, svc, et, row)
    etag = etag_for(et, row)
    if svc.version >= 4:
        response_headers = {"Location": location, "OData-Version": "4.0"}
        if etag:
            response_headers["ETag"] = etag
        if warnings:
            # V4 services carry the same messages on the entity itself
            entity["SAP__Messages"] = [
                {k: v for k, v in m.items() if k != "details"} for m in warnings]
        return _with_messages(Response(
            201, headers=response_headers, body=odata4.entity_envelope(
                entity, _context_for(ctx, svc, set_name, opts, single=True))), warnings)
    response_headers = {"Location": location, "DataServiceVersion": "2.0"}
    if etag:
        response_headers["ETag"] = etag
    return _with_messages(
        Response(201, body=entity_envelope(entity), headers=response_headers), warnings)
