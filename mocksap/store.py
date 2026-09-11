"""CRUD primitives over SQLite for the OData entity sets."""
from __future__ import annotations

import datetime as _dt
import sqlite3
from typing import Any, Dict, List, Optional

from . import db
from .odata import SapError, to_db_value
from .schema import ENTITY_TYPES, EntityType

# Entity types whose key is drawn from a number range on create (as in SAP,
# where the document number is assigned by the system, not by the caller).
AUTO_KEY = {
    "A_SalesOrder": ("SalesOrder", "SALESORDER", 10),
    "A_PurchaseOrder": ("PurchaseOrder", "PURCHASEORDER", 10),
    "A_BusinessPartner": ("BusinessPartner", "BUSINESSPARTNER", 10),
}

# Status and document fields the system sets on creation, as SAP would.
DOCUMENT_DEFAULTS = {
    "A_SalesOrder": {
        "SalesOrderType": "OR",
        "OverallSDProcessStatus": "A",   # not yet processed
        "OverallDeliveryStatus": "A",    # not yet delivered
        "SalesOrderDate": lambda ctx: ctx["today"],
    },
    "A_PurchaseOrder": {
        "PurchaseOrderType": "NB",
        "PurchasingProcessingStatus": "02",
        "PurchaseOrderDate": lambda ctx: ctx["today"],
    },
    "A_SalesOrderItem": {"SalesOrderItemCategory": "TAN"},
}

# Fields the system fills in, mirroring SAP's administrative data.
ADMIN_DEFAULTS = {
    "CreatedByUser": lambda ctx: ctx.get("user", "MOCKUSER"),
    "LastChangedByUser": lambda ctx: ctx.get("user", "MOCKUSER"),
    "CreationDate": lambda ctx: ctx["now"],
    "LastChangeDate": lambda ctx: ctx["now"],
}


def _initial(prop):
    """The ABAP initial value for a property's type."""
    if prop.type == "Edm.DateTime":
        return None
    if prop.type in ("Edm.Decimal", "Edm.Double"):
        return 0.0
    if prop.type in ("Edm.Int32", "Edm.Int16", "Edm.Int64", "Edm.Boolean"):
        return 0
    return ""


def _quote(name: str) -> str:
    return '"%s"' % name


def where_keys(et: EntityType, keys: Dict[str, Any]):
    clause = " AND ".join("%s = ?" % _quote(k) for k in keys)
    return clause, [keys[k] for k in keys]


def get(conn, et: EntityType, keys: Dict[str, Any]) -> Optional[sqlite3.Row]:
    clause, params = where_keys(et, keys)
    return conn.execute(
        'SELECT * FROM "%s" WHERE %s' % (et.name, clause), params
    ).fetchone()


def query(conn, et: EntityType, where: str = "", params=(), order: str = "",
          top: Optional[int] = None, skip: Optional[int] = None) -> List[sqlite3.Row]:
    sql = 'SELECT * FROM "%s"' % et.name
    if where:
        sql += " WHERE " + where
    if order:
        sql += " ORDER BY " + order
    else:
        sql += " ORDER BY " + ", ".join(_quote(k.name) for k in et.keys)
    if top is not None:
        sql += " LIMIT %d" % top
        if skip:
            sql += " OFFSET %d" % skip
    elif skip:
        sql += " LIMIT -1 OFFSET %d" % skip
    return conn.execute(sql, list(params)).fetchall()


def count(conn, et: EntityType, where: str = "", params=()) -> int:
    sql = 'SELECT COUNT(*) AS c FROM "%s"' % et.name
    if where:
        sql += " WHERE " + where
    return conn.execute(sql, list(params)).fetchone()["c"]


def children(conn, parent_row, et: EntityType, nav, limit: Optional[int] = None):
    target = ENTITY_TYPES[nav.target]
    clause = " AND ".join("%s = ?" % _quote(remote) for _l, remote in nav.join)
    params = [parent_row[local] for local, _r in nav.join]
    sql = 'SELECT * FROM "%s" WHERE %s ORDER BY %s' % (
        target.name, clause, ", ".join(_quote(k.name) for k in target.keys))
    if limit:
        sql += " LIMIT %d" % limit
    return conn.execute(sql, params).fetchall()


def _context(user: str) -> dict:
    now = _dt.datetime.utcnow().replace(microsecond=0)
    return {
        "user": user,
        "now": now.isoformat(),
        "today": now.replace(hour=0, minute=0, second=0).isoformat(),
    }


def split_payload(et: EntityType, payload: dict):
    """Separate flat properties from navigation properties (deep insert)."""
    flat, deep = {}, {}
    for key, value in payload.items():
        if key in ("__metadata", "__count", "__deferred"):
            continue
        nav = et.nav(key)
        if nav is not None:
            if isinstance(value, dict) and "results" in value:
                value = value["results"]
            if isinstance(value, dict):
                value = [value]
            if not isinstance(value, list):
                raise SapError("Invalid payload for navigation property '%s'" % key, 400, target=key)
            deep[key] = value
            continue
        if et.prop(key) is None:
            raise SapError(
                "Property '%s' does not exist in type '%s'" % (key, et.name),
                400, target=key)
        flat[key] = value
    return flat, deep


def insert(conn, et: EntityType, payload: dict, user: str = "MOCKUSER",
           parent_keys: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Insert one entity (with children, if the payload is a deep insert)."""
    ctx = _context(user)
    flat, deep = split_payload(et, payload)
    row: Dict[str, Any] = {}

    for name, value in flat.items():
        prop = et.prop(name)
        row[name] = to_db_value(prop, value)

    if parent_keys:
        row.update(parent_keys)

    auto = AUTO_KEY.get(et.name)
    if auto:
        field, obj, width = auto
        if not row.get(field):
            row[field] = db.next_number(conn, obj, width)

    for p in et.keys:
        if not row.get(p.name):
            if p.name in ("SalesOrderItem", "PurchaseOrderItem"):
                row[p.name] = _next_item_number(conn, et, row, p)
                continue
            raise SapError(
                "Key property '%s' is missing in the payload" % p.name,
                400, target=p.name)

    for name, factory in ADMIN_DEFAULTS.items():
        prop = et.prop(name)
        if prop is not None and name not in flat:
            row[name] = factory(ctx)

    for name, default in DOCUMENT_DEFAULTS.get(et.name, {}).items():
        if not row.get(name):
            row[name] = default(ctx) if callable(default) else default

    # SAP hands back initial values, not nulls: '' for characters, 0 for numbers.
    for p in et.props:
        if row.get(p.name) is None:
            row[p.name] = _initial(p)

    keys = {p.name: row[p.name] for p in et.keys}
    if get(conn, et, keys) is not None:
        raise SapError(
            "An entity with the same key already exists in '%s'" % et.name, 409)

    cols = ", ".join(_quote(c) for c in row)
    marks = ", ".join("?" for _ in row)
    conn.execute('INSERT INTO "%s" (%s) VALUES (%s)' % (et.name, cols, marks),
                 list(row.values()))

    for nav_name, items in deep.items():
        nav = et.nav(nav_name)
        target = ENTITY_TYPES[nav.target]
        child_keys = {remote: row[local] for local, remote in nav.join}
        for item in items:
            insert(conn, target, item, user=user, parent_keys=child_keys)

    conn.commit()
    _recalculate_totals(conn, et, row)
    return keys


def _next_item_number(conn, et: EntityType, row: Dict[str, Any], p) -> str:
    """Items are numbered 10, 20, 30 ... within their document, as in SAP."""
    parent = "SalesOrder" if p.name == "SalesOrderItem" else "PurchaseOrder"
    width = 6 if p.name == "SalesOrderItem" else 5
    cur = conn.execute(
        'SELECT MAX(CAST("%s" AS INTEGER)) m FROM "%s" WHERE "%s" = ?'
        % (p.name, et.name, parent), (row.get(parent),)).fetchone()
    highest = cur["m"] or 0
    return str(highest + 10).zfill(width)


def _recalculate_totals(conn, et: EntityType, row: Dict[str, Any]) -> None:
    """Keep TotalNetAmount consistent with the items, like the SD pricing run."""
    if et.name == "A_SalesOrderItem":
        so = row.get("SalesOrder")
        total = conn.execute(
            'SELECT COALESCE(SUM("NetAmount"),0) t FROM "A_SalesOrderItem" WHERE "SalesOrder"=?',
            (so,)).fetchone()["t"]
        conn.execute('UPDATE "A_SalesOrder" SET "TotalNetAmount"=? WHERE "SalesOrder"=?',
                     (round(total, 2), so))
        conn.commit()
    elif et.name == "A_SalesOrder":
        _recalculate_totals(conn, ENTITY_TYPES["A_SalesOrderItem"],
                            {"SalesOrder": row.get("SalesOrder")})


def update(conn, et: EntityType, keys: Dict[str, Any], payload: dict,
           merge: bool = True, user: str = "MOCKUSER") -> None:
    existing = get(conn, et, keys)
    if existing is None:
        raise SapError("Resource not found for the segment '%s'" % et.name, 404)
    ctx = _context(user)
    flat, deep = split_payload(et, payload)
    if deep:
        raise SapError("Deep update of navigation properties is not supported", 501)
    values: Dict[str, Any] = {}
    for name, value in flat.items():
        prop = et.prop(name)
        if prop.key:
            if str(existing[name]) != str(to_db_value(prop, value)):
                raise SapError("Key property '%s' cannot be changed" % name, 400, target=name)
            continue
        values[name] = to_db_value(prop, value)
    if not merge:  # PUT replaces: unspecified, non-key properties are reset
        for p in et.props:
            if not p.key and p.name not in values and p.updatable:
                values[p.name] = _initial(p)
    if et.prop("LastChangeDate") is not None:
        values["LastChangeDate"] = ctx["now"]
    if et.prop("LastChangedByUser") is not None:
        values["LastChangedByUser"] = ctx["user"]
    if not values:
        return
    clause, kparams = where_keys(et, keys)
    sets = ", ".join("%s = ?" % _quote(c) for c in values)
    conn.execute('UPDATE "%s" SET %s WHERE %s' % (et.name, sets, clause),
                 list(values.values()) + kparams)
    conn.commit()
    merged = dict(existing)
    merged.update(values)
    _recalculate_totals(conn, et, merged)


def delete(conn, et: EntityType, keys: Dict[str, Any]) -> None:
    existing = get(conn, et, keys)
    if existing is None:
        raise SapError("Resource not found for the segment '%s'" % et.name, 404)
    clause, params = where_keys(et, keys)
    conn.execute('DELETE FROM "%s" WHERE %s' % (et.name, clause), params)
    # cascade along to-many navigations, as deleting a document does in SAP
    for nav in et.navs:
        if nav.multiplicity != "*":
            continue
        target = ENTITY_TYPES[nav.target]
        cclause = " AND ".join("%s = ?" % _quote(remote) for _l, remote in nav.join)
        conn.execute('DELETE FROM "%s" WHERE %s' % (target.name, cclause),
                     [existing[local] for local, _r in nav.join])
    conn.commit()
    _recalculate_totals(conn, et, dict(existing))
