"""IDoc inbox/outbox.

Accepts inbound IDocs as XML (``ORDERS05``-style) or as EDI_DC40 flat files,
assigns an IDoc number from a number range, stores them with a status record,
and can generate outbound ORDERS05 XML from a stored sales order.
"""
from __future__ import annotations

import datetime as _dt
from typing import Dict, Optional
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

from . import db, store
from .odata import SapError
from .schema import ENTITY_TYPES

STATUS_TEXT = {
    "01": "IDoc generated",
    "03": "Data passed to port OK",
    "12": "Dispatch OK",
    "30": "IDoc ready for dispatch (ALE service)",
    "51": "Application document not posted",
    "53": "Application document posted",
    "56": "IDoc with errors added",
    "62": "IDoc passed to application",
    "64": "IDoc ready to be transferred to application",
    "68": "Error - no further processing",
}

# EDI_DC40 flat-file layout (offset, length) for the fields we surface.
_FLAT_FIELDS = {
    "TABNAM": (0, 10), "MANDT": (10, 3), "DOCNUM": (13, 16), "DOCREL": (29, 4),
    "STATUS": (33, 2), "DIRECT": (35, 1), "OUTMOD": (36, 1), "EXPRSS": (37, 1),
    "TEST": (38, 1), "IDOCTYP": (39, 30), "CIMTYP": (69, 30), "MESTYP": (99, 30),
}


def _local(tag: str) -> str:
    return tag.split("}", 1)[1] if "}" in tag else tag


def parse_xml(body: bytes) -> Dict[str, str]:
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise SapError("The IDoc is not well-formed XML: %s" % exc, 400)
    info = {"idoctyp": _local(root.tag), "mestyp": "", "segments": 0, "direction": "2"}
    for elem in root.iter():
        name = _local(elem.tag)
        if name in ("IDOCTYP", "MESTYP", "SNDPRN", "RCVPRN", "DIRECT", "MANDT"):
            info[name.lower()] = (elem.text or "").strip()
        elif name.startswith("E1") or name.startswith("E2"):
            info["segments"] += 1
    if not info.get("mestyp"):
        info["mestyp"] = info["idoctyp"].rstrip("0123456789") or info["idoctyp"]
    return info


def parse_flat(body: bytes) -> Dict[str, str]:
    text = body.decode("utf-8", "replace")
    first = text.splitlines()[0] if text.splitlines() else ""
    info = {"direction": "2", "segments": max(len(text.splitlines()) - 1, 0)}
    for name, (offset, length) in _FLAT_FIELDS.items():
        info[name.lower()] = first[offset:offset + length].strip()
    if not info.get("idoctyp"):
        raise SapError(
            "The flat IDoc does not start with a valid EDI_DC40 control record", 400)
    return info


def receive(ctx, content_type: str, body: bytes) -> dict:
    """Store an inbound IDoc and return its status record."""
    if not body.strip():
        raise SapError("The IDoc payload is empty", 400)
    is_xml = "xml" in (content_type or "").lower() or body.lstrip()[:1] == b"<"
    info = parse_xml(body) if is_xml else parse_flat(body)

    docnum = db.next_number(ctx.conn, "IDOC", 16)
    status = "53"
    now = _dt.datetime.utcnow().replace(microsecond=0)
    ctx.conn.execute(
        "INSERT INTO idoc(docnum,direction,idoctyp,mestyp,status,status_text,"
        "created_at,content_type,payload) VALUES(?,?,?,?,?,?,?,?,?)",
        (docnum, "2", info.get("idoctyp", ""), info.get("mestyp", ""), status,
         STATUS_TEXT[status], now.isoformat(),
         "xml" if is_xml else "flat", body.decode("utf-8", "replace")),
    )
    ctx.conn.commit()
    return {
        "DOCNUM": docnum,
        "IDOCTYP": info.get("idoctyp", ""),
        "MESTYP": info.get("mestyp", ""),
        "STATUS": status,
        "STATUS_TEXT": STATUS_TEXT[status],
        "DIRECT": "2",
        "SEGMENTS": info.get("segments", 0),
        "CREDAT": now.strftime("%Y%m%d"),
        "CRETIM": now.strftime("%H%M%S"),
        "MANDT": ctx.client,
    }


def receipt_xml(receipt: dict) -> str:
    inner = "".join("<%s>%s</%s>" % (k, escape(str(v)), k) for k, v in receipt.items())
    return ('<?xml version="1.0" encoding="utf-8"?><IDOC_STATUS>%s</IDOC_STATUS>' % inner)


def get(ctx, docnum: str) -> Optional[dict]:
    row = ctx.conn.execute("SELECT * FROM idoc WHERE docnum=?",
                           (docnum.zfill(16),)).fetchone()
    return dict(row) if row else None


def listing(ctx, limit: int = 50, mestyp: str = "") -> list:
    sql = "SELECT docnum,direction,idoctyp,mestyp,status,status_text,created_at FROM idoc"
    params = []
    if mestyp:
        sql += " WHERE mestyp = ?"
        params.append(mestyp.upper())
    sql += " ORDER BY docnum DESC LIMIT ?"
    params.append(limit)
    return [dict(r) for r in ctx.conn.execute(sql, params).fetchall()]


def set_status(ctx, docnum: str, status: str, text: str = "") -> dict:
    record = get(ctx, docnum)
    if record is None:
        raise SapError("IDoc %s does not exist" % docnum, 404)
    text = text or STATUS_TEXT.get(status, "Status set by mock")
    ctx.conn.execute("UPDATE idoc SET status=?, status_text=? WHERE docnum=?",
                     (status, text, record["docnum"]))
    ctx.conn.commit()
    record.update({"status": status, "status_text": text})
    return record


# --------------------------------------------------------------------------
# Outbound generation
# --------------------------------------------------------------------------


def _seg(name: str, fields: Dict[str, str], children: str = "") -> str:
    inner = "".join(
        "<%s>%s</%s>" % (k, escape(str(v)), k) for k, v in fields.items() if v not in (None, ""))
    return '<%s SEGMENT="1">%s%s</%s>' % (name, inner, children, name)


def _control_record(docnum: str, idoctyp: str, mestyp: str, client: str) -> str:
    now = _dt.datetime.utcnow()
    return _seg("EDI_DC40", {
        "TABNAM": "EDI_DC40", "MANDT": client, "DOCNUM": docnum, "DOCREL": "756",
        "STATUS": "30", "DIRECT": "1", "OUTMOD": "2", "IDOCTYP": idoctyp,
        "MESTYP": mestyp, "SNDPOR": "SAPMCK", "SNDPRT": "LS", "SNDPRN": "MCKCLNT100",
        "RCVPOR": "A000000001", "RCVPRT": "LS", "RCVPRN": "PARTNER01",
        "CREDAT": now.strftime("%Y%m%d"), "CRETIM": now.strftime("%H%M%S"),
    })


def generate_orders05(ctx, sales_order: str) -> dict:
    """Render a stored sales order as an outbound ORDERS05 IDoc."""
    so_type = ENTITY_TYPES["A_SalesOrder"]
    row = (store.get(ctx.conn, so_type, {"SalesOrder": sales_order.zfill(10)})
           or store.get(ctx.conn, so_type, {"SalesOrder": sales_order}))
    if row is None:
        raise SapError("Sales order %s does not exist" % sales_order, 404)
    items = store.children(ctx.conn, row, so_type, so_type.nav("to_Item"))

    bp_type = ENTITY_TYPES["A_BusinessPartner"]
    bp = store.get(ctx.conn, bp_type, {"BusinessPartner": row["SoldToParty"]})
    addr = store.children(ctx.conn, bp, bp_type, bp_type.nav("to_BusinessPartnerAddress"),
                          limit=1) if bp else []
    a = addr[0] if addr else None

    docnum = db.next_number(ctx.conn, "IDOC", 16)
    doc_date = str(row["SalesOrderDate"] or "")[:10].replace("-", "")

    segments = [
        _control_record(docnum, "ORDERS05", "ORDERS", ctx.client),
        _seg("E1EDK01", {
            "CURCY": row["TransactionCurrency"], "HWAER": row["TransactionCurrency"],
            "WKURS": "1.00000", "ZTERM": row["CustomerPaymentTerms"],
            "BELNR": row["SalesOrder"], "NTGEW": "%.3f" % (row["TotalNetAmount"] or 0),
        }),
        _seg("E1EDK14", {"QUALF": "008", "ORGID": row["SalesOrganization"]}),
        _seg("E1EDK14", {"QUALF": "007", "ORGID": row["DistributionChannel"]}),
        _seg("E1EDK14", {"QUALF": "006", "ORGID": row["OrganizationDivision"]}),
        _seg("E1EDK14", {"QUALF": "012", "ORGID": row["SalesOrderType"]}),
        _seg("E1EDK03", {"IDDAT": "012", "DATUM": doc_date}),
        _seg("E1EDKA1", {
            "PARVW": "AG", "PARTN": row["SoldToParty"],
            "NAME1": bp["BusinessPartnerFullName"] if bp else "",
            "STRAS": a["StreetName"] if a else "", "HAUSN": a["HouseNumber"] if a else "",
            "ORT01": a["CityName"] if a else "", "PSTLZ": a["PostalCode"] if a else "",
            "LAND1": a["Country"] if a else "", "SPRAS_ISO": "EN",
        }),
    ]
    for it in items:
        child = _seg("E1EDP19", {
            "QUALF": "002", "IDTNR": it["Material"], "KTEXT": it["SalesOrderItemText"]})
        segments.append(_seg("E1EDP01", {
            "POSEX": it["SalesOrderItem"], "MENGE": "%.3f" % (it["RequestedQuantity"] or 0),
            "MENEE": it["RequestedQuantityUnit"], "PSTYV": it["SalesOrderItemCategory"],
            "WERKS": it["Plant"], "NTGEW": "%.3f" % (it["NetAmount"] or 0),
            "CURCY": it["TransactionCurrency"],
        }, child))

    xml = ('<?xml version="1.0" encoding="utf-8"?><ORDERS05><IDOC BEGIN="1">%s</IDOC></ORDERS05>'
           % "".join(segments))

    ctx.conn.execute(
        "INSERT INTO idoc(docnum,direction,idoctyp,mestyp,status,status_text,"
        "created_at,content_type,payload) VALUES(?,?,?,?,?,?,?,?,?)",
        (docnum, "1", "ORDERS05", "ORDERS", "03", STATUS_TEXT["03"],
         _dt.datetime.utcnow().replace(microsecond=0).isoformat(), "xml", xml),
    )
    ctx.conn.commit()
    return {"docnum": docnum, "xml": xml}
