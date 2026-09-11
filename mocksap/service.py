"""The OData V2 request dispatcher.

`dispatch` is deliberately transport agnostic: the HTTP server calls it, and
so does the $batch handler for every sub-request inside a multipart batch.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qsl, unquote

from . import metadata, store
from .odata import (SapError, build_orderby, build_where, collection_envelope,
                    entity_envelope, entity_uri, error_payload, key_predicate,
                    parse_key_predicate, serialize_entity, to_json_value)
from .schema import ENTITY_TYPES, SERVICES, EntityType, Service, service_for_path

JSON_CT = "application/json;charset=utf-8"
XML_CT = "application/xml;charset=utf-8"

SUPPORTED_OPTIONS = {
    "$filter", "$select", "$expand", "$orderby", "$top", "$skip",
    "$inlinecount", "$format", "$count", "$search", "$skiptoken", "$links",
    "sap-client", "sap-language", "saml2", "$callback",
}

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
    def error(cls, err: SapError, fmt: str = "json"):
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

    def __init__(self, conn, base_url, user="MOCKUSER", client="100"):
        self.conn = conn
        self.base_url = base_url
        self.user = user
        self.client = client


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


def _check_options(opts: Dict[str, str]) -> None:
    for key in opts:
        if key.startswith("$") and key not in SUPPORTED_OPTIONS:
            raise SapError("Query option '%s' is not supported" % key, 400)


def _parse_expand(expr: str, et: EntityType) -> Dict[str, dict]:
    tree: Dict[str, dict] = {}
    for path in expr.split(","):
        path = path.strip()
        if not path:
            continue
        node, current_type = tree, et
        for segment in path.split("/"):
            nav = current_type.nav(segment)
            if nav is None:
                raise SapError(
                    "Navigation property '%s' not found in type '%s'"
                    % (segment, current_type.name), 400, target=segment)
            node = node.setdefault(segment, {})
            current_type = ENTITY_TYPES[nav.target]
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
    expanded: Dict[str, Any] = {}
    for nav_name, subtree in expand.items():
        nav = et.nav(nav_name)
        target = ENTITY_TYPES[nav.target]
        target_svc = _service_of(target.name, svc)
        if nav.multiplicity == "*":
            kids = store.children(ctx.conn, row, et, nav)
            expanded[nav_name] = {
                "results": [_render(ctx, k, target, target_svc, None, subtree) for k in kids]
            }
        else:
            keys = {remote: row[local] for local, remote in nav.join}
            parent = store.get(ctx.conn, target, keys)
            expanded[nav_name] = (
                _render(ctx, parent, target, target_svc, None, subtree) if parent else None
            )
    return serialize_entity(row, et, svc, ctx.base_url, select, expanded)


def _service_of(type_name: str, fallback: Service) -> Service:
    if type_name in fallback.sets.values():
        return fallback
    for svc in SERVICES.values():
        if type_name in svc.sets.values():
            return svc
    return fallback


def _select_list(opts, et: EntityType) -> Optional[List[str]]:
    if "$select" not in opts or opts["$select"].strip() in ("", "*"):
        return None
    names = []
    for raw in opts["$select"].split(","):
        name = raw.strip().split("/")[0]
        if not name:
            continue
        if et.prop(name) is None and et.nav(name) is None:
            raise SapError("Property '%s' not found in type '%s'" % (name, et.name),
                           400, target=name)
        names.append(name)
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
    xml = _wants_xml(opts, headers)
    try:
        return _dispatch(ctx, svc, rest, method.upper(), opts, headers, body, xml)
    except SapError as err:
        return Response.error(err, "xml" if xml else "json")


def _dispatch(ctx, svc, rest, method, opts, headers, body, xml) -> Response:
    rest = rest.strip("/")

    if rest in ("", "/"):
        if method != "GET":
            raise SapError("Method %s is not allowed on the service document" % method, 405)
        if xml:
            return Response(body=metadata.service_document_xml(svc, ctx.base_url),
                            content_type="application/atomsvc+xml;charset=utf-8")
        return Response(body=metadata.service_document_json(svc, ctx.base_url))

    if rest == "$metadata":
        if method != "GET":
            raise SapError("Method %s is not allowed on $metadata" % method, 405)
        return Response(body=metadata.metadata_document(svc), content_type=XML_CT,
                        headers={"DataServiceVersion": "2.0"})

    _check_options(opts)
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
            select = _select_list(opts, et)
            expand = _parse_expand(opts.get("$expand", ""), et) if opts.get("$expand") else {}
            return Response(body=entity_envelope(_render(ctx, row, et, svc, select, expand)))
        if method in ("PATCH", "MERGE", "PUT"):
            payload = _json_body(body)
            store.update(ctx.conn, et, keys, payload, merge=(method != "PUT"), user=ctx.user)
            return Response(204, body=b"", content_type=None)
        if method == "DELETE":
            store.delete(ctx.conn, et, keys)
            return Response(204, body=b"", content_type=None)
        raise SapError("Method %s is not allowed on an entity" % method, 405)

    row = store.get(ctx.conn, et, keys)
    if row is None:
        raise SapError(_not_found(et, keys), 404)

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
            expand = _parse_expand(opts.get("$expand", ""), target) if opts.get("$expand") else {}
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
    where, params = _where_from(opts, et)
    where, params = _merge_where(extra_where, extra_params, where, params)
    order = build_orderby(opts["$orderby"], et) if opts.get("$orderby") else ""
    top = _int_option(opts, "$top")
    skip = _int_option(opts, "$skip")
    select = _select_list(opts, et)
    expand = _parse_expand(opts.get("$expand", ""), et) if opts.get("$expand") else {}

    rows = store.query(ctx.conn, et, where, params, order, top, skip)
    total = None
    inline = (opts.get("$inlinecount") or "").lower()
    if inline == "allpages":
        total = store.count(ctx.conn, et, where, params)
    elif inline not in ("", "none"):
        raise SapError("Invalid value '%s' for $inlinecount" % opts["$inlinecount"], 400)

    entities = [_render(ctx, r, et, svc, select, expand) for r in rows]
    return Response(body=collection_envelope(entities, total),
                    headers={"DataServiceVersion": "2.0"})


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


def _create(ctx, svc, et, set_name, opts, body, parent_keys=None) -> Response:
    payload = _json_body(body)
    keys = store.insert(ctx.conn, et, payload, user=ctx.user, parent_keys=parent_keys)
    row = store.get(ctx.conn, et, keys)
    expand = _parse_expand(opts.get("$expand", ""), et) if opts.get("$expand") else {}
    entity = _render(ctx, row, et, svc, None, expand)
    location = entity_uri(ctx.base_url, svc, et, row)
    return Response(201, body=entity_envelope(entity),
                    headers={"Location": location, "DataServiceVersion": "2.0"})
