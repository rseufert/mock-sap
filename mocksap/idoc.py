"""IDoc inbox/outbox.

Accepts inbound IDocs as XML (``ORDERS05``-style) or as EDI_DC40 flat files,
assigns an IDoc number from a number range, stores them with a status record,
and can generate outbound ORDERS05 XML from a stored sales order.
"""
from __future__ import annotations

import datetime as _dt
from typing import Dict, List, Optional
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

from . import db, documents, store
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


def _apply_delivery(ctx, body: bytes) -> List[dict]:
    """Post an inbound DELVRY07 against the sales orders it references.

    Each item segment carries the document it was created from (VGBEL/VGPOS)
    and the quantity delivered, which is enough to move the order's delivery
    status the way posting a delivery would.
    """
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return []

    delivered: Dict[str, Dict[str, float]] = {}
    for element in root.iter():
        if _local(element.tag) != "E1EDL24":
            continue
        values = {_local(child.tag): (child.text or "").strip() for child in element}
        order = values.get("VGBEL", "").strip()
        position = values.get("VGPOS", "").strip()
        if not order or not position:
            continue
        try:
            quantity = float(values.get("LFIMG") or values.get("LGMNG") or 0)
        except ValueError:
            quantity = 0.0
        delivered.setdefault(order, {})[position] = quantity

    # the delivery the IDoc describes, if it names one
    announced = ""
    for element in root.iter():
        if _local(element.tag) == "E1EDL20":
            for child in element:
                if _local(child.tag) == "VBELN":
                    announced = (child.text or "").strip()
            break

    applied = []
    for order, positions in delivered.items():
        row = documents.sales_order(ctx, order)
        if row is None:
            applied.append({"SALESORDER": order, "STATUS": "",
                            "MESSAGE": "Sales order %s does not exist" % order})
            continue

        items = {item["SalesOrderItem"]: item for item in documents.order_items(ctx, row)}
        known = announced and store.get(
            ctx.conn, ENTITY_TYPES["A_OutbDeliveryHeader"],
            {"DeliveryDocument": announced.zfill(10)}) is not None

        created = ""
        if not known:
            lines = [(items[position], quantity)
                     for position, quantity in positions.items() if position in items]
            if lines:
                created = documents.create_delivery(ctx, row, lines)

        status = documents.apply_delivery_status(ctx, row, positions)
        entry = {
            "SALESORDER": row["SalesOrder"], "STATUS": status,
            "MESSAGE": "Delivery status set to %s (%s)"
                       % (status, "fully delivered" if status == "C" else "partly delivered"),
        }
        if created:
            entry["DELIVERY"] = created
            entry["MESSAGE"] = "Delivery %s created; %s" % (created, entry["MESSAGE"][0].lower()
                                                            + entry["MESSAGE"][1:])
        applied.append(entry)
    return applied


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
    receipt = {
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

    # a delivery is not just filed: posting it moves the order it came from
    if is_xml and info.get("mestyp", "").upper().startswith("DELVRY"):
        applied = _apply_delivery(ctx, body)
        if applied:
            receipt["APPLIED"] = applied
    return receipt


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


def _sales_order_context(ctx, sales_order: str):
    """The order, its items and its sold-to party, or a 404."""
    so_type = ENTITY_TYPES["A_SalesOrder"]
    row = (store.get(ctx.conn, so_type, {"SalesOrder": sales_order.zfill(10)})
           or store.get(ctx.conn, so_type, {"SalesOrder": sales_order}))
    if row is None:
        raise SapError("Sales order %s does not exist" % sales_order, 404)
    items = store.children(ctx.conn, row, so_type, so_type.nav("to_Item"))
    bp_type = ENTITY_TYPES["A_BusinessPartner"]
    partner = store.get(ctx.conn, bp_type, {"BusinessPartner": row["SoldToParty"]})
    addresses = store.children(ctx.conn, partner, bp_type,
                               bp_type.nav("to_BusinessPartnerAddress"),
                               limit=1) if partner else []
    return row, items, partner, (addresses[0] if addresses else None)


def _partner_segment(role: str, number: str, partner, address) -> str:
    return _seg("E1EDKA1", {
        "PARVW": role, "PARTN": number,
        "NAME1": partner["BusinessPartnerFullName"] if partner else "",
        "STRAS": address["StreetName"] if address else "",
        "HAUSN": address["HouseNumber"] if address else "",
        "ORT01": address["CityName"] if address else "",
        "PSTLZ": address["PostalCode"] if address else "",
        "LAND1": address["Country"] if address else "",
        "SPRAS_ISO": "EN",
    })


def _store_idoc(ctx, docnum, idoctyp, mestyp, xml, direction="1", status="03") -> None:
    ctx.conn.execute(
        "INSERT INTO idoc(docnum,direction,idoctyp,mestyp,status,status_text,"
        "created_at,content_type,payload) VALUES(?,?,?,?,?,?,?,?,?)",
        (docnum, direction, idoctyp, mestyp, status, STATUS_TEXT[status],
         _dt.datetime.utcnow().replace(microsecond=0).isoformat(), "xml", xml),
    )
    ctx.conn.commit()


def generate_invoic02(ctx, sales_order: str) -> dict:
    """Render a stored sales order as an outbound INVOIC02, the way a billing
    document for it would leave the system."""
    row, items, partner, address = _sales_order_context(ctx, sales_order)
    # the invoice is a document, not a number: create it, then render it
    invoice = documents.create_billing_document(ctx, row, items)
    docnum = db.next_number(ctx.conn, "IDOC", 16)
    billing = invoice["billing_document"]
    doc_date = str(row["SalesOrderDate"] or "")[:10].replace("-", "")
    today = _dt.datetime.utcnow().strftime("%Y%m%d")
    currency = row["TransactionCurrency"]
    net, tax = invoice["net"], invoice["tax"]

    segments = [
        _control_record(docnum, "INVOIC02", "INVOIC", ctx.client),
        _seg("E1EDK01", {
            "CURCY": currency, "HWAER": currency, "WKURS": "1.00000",
            "ZTERM": row["CustomerPaymentTerms"], "BELNR": billing,
            "NTGEW": "%.3f" % net, "BSART": "INVO",
        }),
        _seg("E1EDK02", {"QUALF": "009", "BELNR": billing, "DATUM": today}),
        _seg("E1EDK02", {"QUALF": "001", "BELNR": row["SalesOrder"], "DATUM": doc_date}),
        _seg("E1EDK03", {"IDDAT": "026", "DATUM": today}),
        _seg("E1EDK03", {"IDDAT": "012", "DATUM": doc_date}),
        _partner_segment("RE", row["SoldToParty"], partner, address),
        _partner_segment("RG", row["SoldToParty"], partner, address),
        _partner_segment("AG", row["SoldToParty"], partner, address),
    ]
    for item in items:
        quantity = float(item["RequestedQuantity"] or 0)
        amount = float(item["NetAmount"] or 0)
        price = round(amount / quantity, 2) if quantity else amount
        children = "".join([
            _seg("E1EDP19", {"QUALF": "002", "IDTNR": item["Material"],
                             "KTEXT": item["SalesOrderItemText"]}),
            _seg("E1EDP26", {"QUALF": "003", "BETRG": "%.2f" % amount}),
            _seg("E1EDP26", {"QUALF": "011", "BETRG": "%.2f" % price}),
        ])
        segments.append(_seg("E1EDP01", {
            "POSEX": item["SalesOrderItem"], "MENGE": "%.3f" % quantity,
            "MENEE": item["RequestedQuantityUnit"], "PSTYV": item["SalesOrderItemCategory"],
            "WERKS": item["Plant"], "VPREI": "%.2f" % price, "NETWR": "%.2f" % amount,
            "CURCY": currency, "VGBEL": row["SalesOrder"], "VGPOS": item["SalesOrderItem"],
        }, children))
    segments.append(_seg("E1EDS01", {"SUMID": "010", "SUMME": "%.2f" % (net + tax),
                                     "SUNIT": currency}))
    segments.append(_seg("E1EDS01", {"SUMID": "011", "SUMME": "%.2f" % net,
                                     "SUNIT": currency}))
    segments.append(_seg("E1EDS01", {"SUMID": "205", "SUMME": "%.2f" % tax,
                                     "SUNIT": currency}))

    xml = ('<?xml version="1.0" encoding="utf-8"?><INVOIC02><IDOC BEGIN="1">%s'
           "</IDOC></INVOIC02>" % "".join(segments))
    _store_idoc(ctx, docnum, "INVOIC02", "INVOIC", xml)
    return {"docnum": docnum, "xml": xml, "billing_document": billing,
            "accounting_document": invoice["accounting_document"]}


def generate_delvry07(ctx, sales_order: str) -> dict:
    """Render a delivery for a stored sales order as an outbound DELVRY07."""
    row, items, partner, address = _sales_order_context(ctx, sales_order)
    # likewise the delivery: it is created, and the IDoc describes it
    delivery = documents.create_delivery(
        ctx, row, [(item, float(item["RequestedQuantity"] or 0)) for item in items])
    docnum = db.next_number(ctx.conn, "IDOC", 16)
    today = _dt.datetime.utcnow().strftime("%Y%m%d")
    weight = sum(float(item["RequestedQuantity"] or 0) for item in items)

    children = "".join([
        _seg("E1EDL21", {"LFART": "LF", "VSTEL": "1710", "VKORG": row["SalesOrganization"],
                         "ROUTE": "R00001", "BTGEW": "%.3f" % weight, "GEWEI": "KGM"}),
        _seg("E1EDL22", {"VBELN": delivery, "VSTEL": "1710", "LFDAT": today,
                         "LFUHR": "120000", "KODAT": today}),
        _seg("E1ADRM1", {"PARTNER_Q": "WE", "PARTNER_ID": row["SoldToParty"],
                         "NAME1": partner["BusinessPartnerFullName"] if partner else "",
                         "STREET1": address["StreetName"] if address else "",
                         "CITY1": address["CityName"] if address else "",
                         "POSTL_COD1": address["PostalCode"] if address else "",
                         "COUNTRY1": address["Country"] if address else ""}),
    ])
    item_segments = []
    for item in items:
        quantity = float(item["RequestedQuantity"] or 0)
        item_segments.append(_seg("E1EDL24", {
            "POSNR": item["SalesOrderItem"], "MATNR": item["Material"],
            "WERKS": item["Plant"], "LFIMG": "%.3f" % quantity,
            "VRKME": item["RequestedQuantityUnit"], "LGMNG": "%.3f" % quantity,
            "MEINS": item["RequestedQuantityUnit"],
            "ARKTX": item["SalesOrderItemText"],
            "VGBEL": row["SalesOrder"], "VGPOS": item["SalesOrderItem"],
        }, _seg("E1EDL18", {"QUALF": "ORI", "PARAM": "SALESORDER"})))

    segments = [
        _control_record(docnum, "DELVRY07", "DELVRY", ctx.client),
        _seg("E1EDL20", {"VBELN": delivery, "VSTEL": "1710", "VKORG": row["SalesOrganization"],
                         "LFART": "LF", "KUNNR": row["SoldToParty"],
                         "BTGEW": "%.3f" % weight, "GEWEI": "KGM",
                         "ANZPK": str(len(items)).zfill(5)},
             children + "".join(item_segments)),
    ]
    xml = ('<?xml version="1.0" encoding="utf-8"?><DELVRY07><IDOC BEGIN="1">%s'
           "</IDOC></DELVRY07>" % "".join(segments))
    _store_idoc(ctx, docnum, "DELVRY07", "DELVRY", xml)
    return {"docnum": docnum, "xml": xml, "delivery": delivery}


GENERATORS = {
    "ORDERS": ("ORDERS05", lambda ctx, order: generate_orders05(ctx, order)),
    "ORDERS05": ("ORDERS05", lambda ctx, order: generate_orders05(ctx, order)),
    "INVOIC": ("INVOIC02", generate_invoic02),
    "INVOIC02": ("INVOIC02", generate_invoic02),
    "DELVRY": ("DELVRY07", generate_delvry07),
    "DELVRY07": ("DELVRY07", generate_delvry07),
}


def generate(ctx, mestyp: str, sales_order: str) -> dict:
    """Generate an outbound IDoc of the requested message type."""
    entry = GENERATORS.get((mestyp or "ORDERS").upper())
    if entry is None:
        raise SapError(
            "Message type %s cannot be generated; this mock generates %s"
            % (mestyp, ", ".join(sorted({v[0] for v in GENERATORS.values()}))), 400)
    return entry[1](ctx, sales_order)
