"""OData V4 payload shaping, as the S/4HANA `odata4` services speak it.

Everything below the wire - the schema, the store, the `$filter` compiler - is
shared with V2.  Only the shapes differ, and they differ a lot: no `d`
envelope, no `__metadata`, annotations instead (`@odata.context`,
`@odata.count`, `@odata.etag`), ISO timestamps rather than `/Date(ms)/`, and
numbers rather than quoted decimals.
"""
from __future__ import annotations

import datetime as _dt
from typing import Any, Dict, List, Optional

from .odata import SapError, _parse_datetime, row_get
from .schema import COMPLEX_TYPES, EntityType, Prop, Service, set_for_type


def to_json_value(prop: Prop, value: Any) -> Any:
    """Render a stored value the way V4 JSON does."""
    if value is None:
        return None
    t = prop.type
    if t == "Edm.Boolean":
        return bool(value)
    if t in ("Edm.Int32", "Edm.Int16", "Edm.Int64"):
        return int(value)
    if t in ("Edm.Decimal", "Edm.Double"):
        return float(value)  # a number, not a string, unlike V2
    if t == "Edm.DateTime":  # V4 calls it Edm.DateTimeOffset
        moment = _parse_datetime(value)
        if moment is None:
            return None
        return moment.replace(tzinfo=_dt.timezone.utc).isoformat().replace("+00:00", "Z")
    return str(value)


def edm_type(prop: Prop) -> str:
    """The V4 spelling of a property's type."""
    if prop.type == "Edm.DateTime":
        return "Edm.DateTimeOffset"
    return prop.type


def context_url(base_url: str, svc: Service, fragment: str) -> str:
    return "%s%s/$metadata#%s" % (base_url, svc.path, fragment)


def _complex_value(prop: Prop, row, subs) -> dict:
    ct = COMPLEX_TYPES[prop.complex_type]
    value: Dict[str, Any] = {}
    for sub in ct.props:
        if subs and sub.name not in subs:
            continue
        value[sub.name] = to_json_value(sub, row_get(row, "%s_%s" % (prop.name, sub.name)))
    return value


def _selected(select, name):
    if select is None:
        return []
    subs = []
    for entry in select:
        if entry == name:
            return []
        if entry.startswith(name + "/"):
            subs.append(entry.split("/", 1)[1])
    return subs or None


def serialize_entity(row, et: EntityType, svc: Service, base_url: str,
                     select: Optional[List[str]] = None,
                     expand: Optional[Dict[str, Any]] = None,
                     etag: Optional[str] = None) -> dict:
    """One entity in V4 shape: properties only, plus annotations.

    Navigation properties that were not expanded are simply absent - V4 has
    no `__deferred`, and SAP does not emit navigation links at the default
    metadata level.
    """
    out: Dict[str, Any] = {}
    if etag:
        out["@odata.etag"] = etag
    for p in et.props:
        chosen = _selected(select, p.name)
        if select is not None and chosen is None:
            continue
        if p.complex_type:
            out[p.name] = _complex_value(p, row, chosen)
        else:
            out[p.name] = to_json_value(p, row_get(row, p.name))
    for name, value in (expand or {}).items():
        out[name] = value
    return out


def collection_envelope(entities: List[dict], context: str,
                        count: Optional[int] = None) -> dict:
    body: Dict[str, Any] = {"@odata.context": context}
    if count is not None:
        body["@odata.count"] = count
    body["value"] = entities
    return body


def entity_envelope(entity: dict, context: str) -> dict:
    body = {"@odata.context": context}
    body.update(entity)
    return body


def service_document(svc: Service, base_url: str) -> dict:
    return {
        "@odata.context": "%s%s/$metadata" % (base_url, svc.path),
        "value": [
            {"name": name, "kind": "EntitySet", "url": name} for name in svc.sets
        ],
    }


def error_payload(err: SapError) -> dict:
    """The V4 error object: a flat message string and a details array."""
    return {
        "error": {
            "code": err.code,
            "message": err.message,
            "target": err.target or None,
            "details": [
                {
                    "code": err.code,
                    "message": err.message,
                    "target": err.target or None,
                    "@Common.numericSeverity": 4,
                }
            ],
            "@Common.ExceptionCategory": "Processing_Error",
        }
    }


def entity_ref(base_url: str, svc: Service, et: EntityType, row) -> str:
    """The `@odata.id` of a row: what $ref answers with."""
    from .odata import key_predicate

    return "%s%s/%s%s" % (base_url, svc.path, set_for_type(svc, et.name),
                          key_predicate(et, row))
