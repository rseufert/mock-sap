"""Messages that travel with a successful response.

SAP answers are not binary.  A request can succeed and still carry warnings -
a date that was rescheduled, a material flagged for deletion - in the
`sap-message` header, and on V4 services in a `SAP__Messages` collection on
the entity.  SAPUI5 shows those in its message popover without treating them
as failures.

The rules below are the mock's own, small set: enough that a client can be
built and tested against warnings that arise from real data rather than only
from injection.  The message classes and numbers are plausible rather than
authentic - what a client depends on is the shape, the severity and the
target.
"""
from __future__ import annotations

import datetime as _dt
import json
from typing import Any, Dict, List, Optional

from . import store
from .schema import ENTITY_TYPES, EntityType

# numericSeverity follows SAP's convention: 1 success, 2 info, 3 warning, 4 error
SEVERITY = {"success": 1, "info": 2, "warning": 3, "error": 4}


def message(code: str, text: str, severity: str = "warning",
            target: str = "", transition: bool = False) -> Dict[str, Any]:
    return {
        "code": code,
        "message": text,
        "severity": severity,
        "target": target,
        "transition": transition,
        "numericSeverity": SEVERITY.get(severity, 3),
        "details": [],
    }


def to_header(messages: List[dict]) -> Optional[str]:
    """Render messages the way the `sap-message` header carries them.

    One message is the object itself; several are the first one with the rest
    hanging off its `details`, which is how Gateway packs them.
    """
    if not messages:
        return None
    head = dict(messages[0])
    head["details"] = [dict(m, details=[]) for m in messages[1:]]
    return json.dumps(head, separators=(",", ":"))


# --------------------------------------------------------------------------
# The rules
# --------------------------------------------------------------------------


def after_write(conn, et: EntityType, keys: Dict[str, Any],
                today: Optional[_dt.date] = None) -> List[dict]:
    """Inspect what was just written, and warn where SAP would warn.

    A rule may correct the document as well as complain about it - SAP moves a
    delivery date it cannot honour rather than refusing the order - so this
    runs after the write and may write again.
    """
    today = today or _dt.datetime.utcnow().date()
    if et.name == "A_SalesOrder":
        return _sales_order(conn, et, keys, today)
    if et.name == "A_SalesOrderItem":
        return _sales_order_item(conn, et, keys)
    return []


def _sales_order(conn, et: EntityType, keys, today: _dt.date) -> List[dict]:
    row = store.get(conn, et, keys)
    if row is None:
        return []
    out: List[dict] = []

    requested = str(row["RequestedDeliveryDate"] or "")[:10]
    if requested and requested < today.isoformat():
        moved = today.isoformat() + "T00:00:00"
        conn.execute('UPDATE "A_SalesOrder" SET "RequestedDeliveryDate"=? '
                     'WHERE "SalesOrder"=?', (moved, row["SalesOrder"]))
        conn.commit()
        out.append(message(
            "V1/302",
            "Requested delivery date %s is in the past; it was moved to %s"
            % (requested, today.isoformat()),
            target="RequestedDeliveryDate"))

    partner = store.get(conn, ENTITY_TYPES["A_BusinessPartner"],
                        {"BusinessPartner": row["SoldToParty"]})
    if partner is not None and partner["BusinessPartnerIsBlocked"]:
        out.append(message(
            "F2/145", "Sold-to party %s is blocked centrally" % row["SoldToParty"],
            target="SoldToParty"))
    return out


def _sales_order_item(conn, et: EntityType, keys) -> List[dict]:
    row = store.get(conn, et, keys)
    if row is None or not row["Material"]:
        return []
    product = store.get(conn, ENTITY_TYPES["A_Product"], {"Product": row["Material"]})
    if product is None:
        return []
    if product["IsMarkedForDeletion"]:
        return [message(
            "M3/808", "Material %s is flagged for deletion" % row["Material"],
            target="Material")]
    return []
