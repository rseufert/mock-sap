"""IDoc inbox/outbox.

Accepts inbound IDocs as XML (``ORDERS05``-style) or as EDI_DC40 flat files,
assigns an IDoc number from a number range, stores them with a status record,
and can generate outbound ORDERS05 XML from a stored sales order.
"""
from __future__ import annotations

import datetime as _dt
import threading
from decimal import Decimal
from typing import Any, Dict, List, Optional
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

from . import clock, db, documents, money, outcome, reconcile, store
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

# What posting an inbound IDoc can end in. The rest of STATUS_TEXT describes an
# outbound IDoc on its way to a port, which is not an outcome of posting one.
INBOUND_STATUSES = ("53", "51", "56", "68")


class NotPosted(Exception):
    """The IDoc arrived and the application declined to post it.

    Raised by the ``_apply_*`` functions when they cannot do what posting the
    IDoc means: an ``INVOIC`` that names no supplier owes nobody money, and a
    ``DELVRY`` that names no order line delivers nothing.  ``receive`` turns
    this into status 51 with the reason as its text, because reporting 53 --
    *Application document posted* -- for an IDoc that posted nothing tells a
    client the opposite of what happened, and leaves it no way to find out.

    This is the mock being honest about its own limits, not a fault injected on
    request.  A client that wants a refusal on demand uses ``/_mock/idoc-posting``.
    """


class PostingRules:
    """What the application does with an inbound IDoc, driven through
    ``/_mock/idoc-posting``.

    Receiving an IDoc and posting it are two different events, and SAP reports
    them separately: the port answers, and then the application either posts
    the document or does not. Without this, every IDoc this mock receives
    posts, so the failure that costs the most -- accepted, never posted, nobody
    told -- is the one failure a client could not be tested against.
    """

    def __init__(self):
        self.rules: List[dict] = []
        self._lock = threading.Lock()
        self._next_id = 1

    def add(self, rule: dict) -> dict:
        status = str(rule.get("status") or "51")
        if status not in INBOUND_STATUSES:
            raise SapError(
                "Posting an inbound IDoc cannot end in status %s; this mock "
                "offers %s" % (status, ", ".join(
                    "%s (%s)" % (s, STATUS_TEXT[s]) for s in INBOUND_STATUSES)), 400)
        with self._lock:
            stored = {
                "id": self._next_id,
                "mestyp": str(rule.get("mestyp") or "").upper(),
                "idoctyp": str(rule.get("idoctyp") or "").upper(),
                "status": status,
                "message": str(rule.get("message") or rule.get("text") or ""),
                "count": int(rule.get("count") or 0),   # 0 = until cleared
                "hits": 0,
            }
            self._next_id += 1
            self.rules.append(stored)
            return stored

    def clear(self) -> int:
        with self._lock:
            count = len(self.rules)
            self.rules = []
            return count

    def match(self, mestyp: str, idoctyp: str) -> Optional[dict]:
        """The first rule that applies, spent if it was only good for so many."""
        with self._lock:
            for rule in list(self.rules):
                if rule["mestyp"] and rule["mestyp"] != (mestyp or "").upper():
                    continue
                if rule["idoctyp"] and rule["idoctyp"] != (idoctyp or "").upper():
                    continue
                rule["hits"] += 1
                if rule["count"] and rule["hits"] >= rule["count"]:
                    self.rules.remove(rule)
                return rule
        return None

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


def _apply_delivery(ctx, body: bytes, docnum: str = "") -> List[dict]:
    """Post an inbound DELVRY07 against the sales orders it references.

    Each item segment carries the document it was created from (VGBEL/VGPOS)
    and the quantity delivered, which is enough to move the order's delivery
    status the way posting a delivery would.
    """
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise NotPosted("The DELVRY is not well-formed XML: %s" % exc)

    delivered: Dict[str, Dict[str, Any]] = {}
    for element in root.iter():
        if _local(element.tag) != "E1EDL24":
            continue
        values = {_local(child.tag): (child.text or "").strip() for child in element}
        order = values.get("VGBEL", "").strip()
        position = values.get("VGPOS", "").strip()
        if not order or not position:
            continue
        try:
            quantity = money.of(values.get("LFIMG") or values.get("LGMNG"))
        except ValueError:
            quantity = Decimal(0)
        delivered.setdefault(order, {})[position] = quantity

    # the delivery the IDoc describes, if it names one
    announced = ""
    for element in root.iter():
        if _local(element.tag) == "E1EDL20":
            for child in element:
                if _local(child.tag) == "VBELN":
                    announced = (child.text or "").strip()
            break

    if not delivered:
        raise NotPosted("No E1EDL24 item segment names a document and position "
                        "(VGBEL/VGPOS), so this DELVRY delivers nothing")

    applied = []
    for order, positions in delivered.items():
        row = documents.sales_order(ctx, order)
        if row is None:
            applied.append({"SALESORDER": order, "STATUS": "",
                            "MESSAGE": outcome.delivery_message(order, "", "")})
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
            "MESSAGE": outcome.delivery_message(row["SalesOrder"], status, created),
        }
        if created:
            entry["DELIVERY"] = created
        applied.append(entry)
    if docnum:
        outcome.file_delivery(ctx.conn, docnum, applied)
    return applied


def _idoc_date(value: str) -> str:
    """YYYYMMDD off the wire, ISO in the database."""
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())[:8]
    if len(digits) != 8:
        return ""
    return "%s-%s-%s" % (digits[:4], digits[4:6], digits[6:])


def _apply_invoice(ctx, body: bytes, docnum: str = "") -> List[dict]:
    """Post an inbound INVOIC as a supplier invoice with an open payable.

    The segments are the ones this mock writes on the way out, read from the
    other side: E1EDK01 carries the currency and the terms, an E1EDKA1 with
    PARVW ``LF`` says who billed us, E1EDK02 ``009`` is the supplier's own
    invoice number, the E1EDS01 sums are the totals, and each E1EDP01 names
    the purchase order it bills against.
    """
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise NotPosted("The INVOIC is not well-formed XML: %s" % exc)

    header, sums, partners, items = {}, {}, {}, []
    references, dates = {}, {}
    for element in root.iter():
        tag = _local(element.tag)
        values = {_local(child.tag): (child.text or "").strip()
                  for child in element}
        if tag == "E1EDK01":
            header = values
        elif tag == "E1EDKA1":
            partners.setdefault(values.get("PARVW", ""), values)
        elif tag == "E1EDK02":
            references.setdefault(values.get("QUALF", ""), values)
        elif tag == "E1EDK03":
            dates.setdefault(values.get("IDDAT", ""), values)
        elif tag == "E1EDS01":
            sums[values.get("SUMID", "")] = values
        elif tag == "E1EDP01":
            item = dict(values)
            for child in element:
                if _local(child.tag) == "E1EDP19":
                    item.setdefault("IDTNR", "")
            items.append(item)

    vendor = partners.get("LF") or partners.get("RS") or {}
    supplier = (vendor.get("LIFNR") or vendor.get("PARTN") or "").strip()
    if not supplier:
        raise NotPosted("No E1EDKA1 segment with PARVW LF or RS names the "
                        "supplier who billed us, so there is nobody to owe")

    def amount(sumid):
        # None for a sum the IDoc does not state, which is not a sum of zero:
        # an invoice that gives only its total has not claimed to be tax free.
        raw = (sums.get(sumid) or {}).get("SUMME", "")
        try:
            return money.of(raw) if raw else None
        except ValueError:
            return None

    gross = amount("010")
    net = amount("011")
    tax = amount("205")
    if not gross:
        # An invoice with no total is not an invoice: posting one would owe the
        # supplier nothing, which is indistinguishable from not posting it.
        raise NotPosted("No E1EDS01 segment with SUMID 010 gives an invoice "
                        "total, so this INVOIC bills nothing")

    # Eight digits that are no day of any month - 20261345 - used to get as
    # far as the due date and fail there, with the document half written.
    dated = _idoc_date(dates.get("026", {}).get("DATUM"))
    try:
        dated = documents.as_date(dated).isoformat() if dated else None
    except ValueError:
        raise NotPosted("E1EDK03 IDDAT 026 gives the invoice date as %s, which "
                        "is not a date, so there is no day this INVOIC falls "
                        "due from" % dates["026"]["DATUM"])

    invoice = {
        "supplier": supplier,
        "reference": (references.get("009", {}).get("BELNR")
                      or header.get("BELNR") or ""),
        "currency": header.get("CURCY") or "EUR",
        "terms": header.get("ZTERM") or "",
        "gross": gross, "net": net, "tax": tax,
        "document_date": dated,
        "baseline_date": dated,
        "items": [{
            "purchase_order": item.get("VGBEL", ""),
            "purchase_order_item": item.get("VGPOS", ""),
            "amount": item.get("NETWR") or None,
            "quantity": item.get("MENGE") or 0,
            "unit": item.get("MENEE", ""),
            "text": item.get("KTEXT", ""),
        } for item in items],
    }
    try:
        posted = documents.post_supplier_invoice(ctx, invoice)
    except documents.Unbalanced as unbalanced:
        # FI writes no document that does not balance, and the IDoc is where
        # the supplier's arithmetic arrives to be found out.
        raise NotPosted("%s, so this INVOIC posts nothing (E1EDS01 SUMID 010, "
                        "011 and 205; E1EDP01 NETWR)" % unbalanced.message)
    gross_text = money.text(posted["gross"], money.CURRENCY_SCALE)
    applied = [{
        "SUPPLIERINVOICE": posted["supplier_invoice"],
        "FISCALYEAR": posted["fiscal_year"],
        "ACCOUNTINGDOCUMENT": posted["accounting_document"],
        "INVOICINGPARTY": supplier,
        "MESSAGE": outcome.invoice_message(posted["supplier_invoice"],
                                           posted["currency"], gross_text,
                                           supplier),
    }]
    if docnum:
        outcome.file_invoice(ctx.conn, docnum, applied, posted["currency"],
                             gross_text)
    return applied


def receive(ctx, content_type: str, body: bytes, posting=None) -> dict:
    """Store an inbound IDoc and return its status record.

    ``posting`` is an optional :class:`PostingRules`: the IDoc is received
    either way -- that is what the receipt says -- but the application may
    decline to post it, and then none of what posting it would have done
    happens.
    """
    if not body.strip():
        raise SapError("The IDoc payload is empty", 400)
    is_xml = "xml" in (content_type or "").lower() or body.lstrip()[:1] == b"<"
    info = parse_xml(body) if is_xml else parse_flat(body)

    docnum = db.next_number(ctx.conn, "IDOC", 16)
    rule = posting.match(info.get("mestyp", ""), info.get("idoctyp", "")) if posting else None
    status = rule["status"] if rule else "53"
    status_text = (rule and rule["message"]) or STATUS_TEXT[status]

    # Post before the IDoc is filed, so the status stored is the one that
    # happened. An application that declines says why, and 51 carries the
    # reason; deciding the status first and applying afterwards is how an IDoc
    # that posted nothing came to be filed as "Application document posted".
    applied = None
    if status == "53":
        try:
            applied = _apply(ctx, info, body, is_xml, docnum)
        except NotPosted as declined:
            status, status_text = "51", str(declined)

    now = clock.now().replace(microsecond=0)
    ctx.conn.execute(
        "INSERT INTO idoc(docnum,direction,idoctyp,mestyp,status,status_text,"
        "created_at,content_type,payload) VALUES(?,?,?,?,?,?,?,?,?)",
        (docnum, "2", info.get("idoctyp", ""), info.get("mestyp", ""), status,
         status_text, now.isoformat() + "Z",
         "xml" if is_xml else "flat", body.decode("utf-8", "replace")),
    )
    ctx.conn.commit()
    receipt = {
        "DOCNUM": docnum,
        "IDOCTYP": info.get("idoctyp", ""),
        "MESTYP": info.get("mestyp", ""),
        "STATUS": status,
        "STATUS_TEXT": status_text,
        "DIRECT": "2",
        "SEGMENTS": info.get("segments", 0),
        "CREDAT": now.strftime("%Y%m%d"),
        "CRETIM": now.strftime("%H%M%S"),
        "MANDT": ctx.client,
    }

    if applied:
        receipt["APPLIED"] = applied
    return receipt


def _apply(ctx, info: dict, body: bytes, is_xml: bool,
           docnum: str = "") -> Optional[List[dict]]:
    """Do what posting this IDoc means, or raise NotPosted saying why not.

    Only the message types with something to post are here. An ORDERS05 is
    filed and nothing else, so it has no entry and posts as 53 on its own.

    Posting reads the segments, and reading them needs the XML: a flat-file
    IDoc would need every segment's fixed-width layout, which this mock does
    not have. So a flat one that would post something is refused by name rather
    than filed as posted, the way ``statement.parse`` already refuses a flat
    FINSTA01.
    """
    mestyp = info.get("mestyp", "").upper()
    posts = ("DELVRY", "INVOIC", "FINSTA")
    if not is_xml and mestyp.startswith(posts):
        raise NotPosted("A %s can only be posted as IDoc XML; this one arrived "
                        "as a flat file, whose segment layouts this mock does "
                        "not have" % mestyp)
    # A delivery is not just filed: posting it moves the order it came from.
    # An IDoc that did not post has done nothing to the order, which is the
    # whole difference between status 53 and status 51.
    if mestyp.startswith("DELVRY"):
        return _apply_delivery(ctx, body, docnum)
    # An INVOIC is a bill: posting it owes somebody money, and that payable is
    # what a payment run later selects. A failed posting owes nobody anything.
    if mestyp.startswith("INVOIC"):
        return _apply_invoice(ctx, body, docnum)
    # A FINSTA is the bank telling us what happened to the money. Posting it
    # clears what it paid and reopens what came back; a failed posting clears
    # nothing, because an item cleared by an IDoc that did not post would be
    # an invoice nobody can find and nobody will pay again.
    if mestyp.startswith("FINSTA"):
        return [reconcile.apply_statement(ctx, body, docnum)]
    return None


def receipt_xml(receipt: dict) -> str:
    inner = "".join("<%s>%s</%s>" % (k, escape(str(v)), k) for k, v in receipt.items())
    return ('<?xml version="1.0" encoding="utf-8"?><IDOC_STATUS>%s</IDOC_STATUS>' % inner)


def get(ctx, docnum: str) -> Optional[dict]:
    """One IDoc, and - if posting it decided anything - what that was.

    ``APPLIED`` is the same list the POST receipt carried, read back from where
    posting filed it rather than recomputed, so the two agree: the statement's
    lines (#105), the sales orders a DELVRY moved or could not find, or the
    invoice and payable an INVOIC posted (#116). An ORDERS05 is only filed, so
    it has no outcome and the key is absent rather than empty.

    It is on the by-docnum read and deliberately not on the listing: one IDoc
    has one outcome, where a listing of 50 would carry 50.
    """
    row = ctx.conn.execute("SELECT * FROM idoc WHERE docnum=?",
                           (docnum.zfill(16),)).fetchone()
    if row is None:
        return None
    record = dict(row)
    applied = outcome.of(ctx.conn, record["docnum"])
    if applied is not None:
        record["APPLIED"] = applied
    return record


def listing(ctx, limit: int = 50, mestyp: str = "", settled: str = "") -> list:
    """The IDocs, newest first, seven columns and no payload.

    ``settled`` narrows to the IDocs whose statement named one accounting
    document - the reverse of the by-docnum read, and the question an
    integration actually asks: not "what did this IDoc do?" but "what settled
    my invoice?" Without it that answer costs a read of every IDoc, which is
    the reason the outcome is a table and not a column (#105).
    """
    sql = "SELECT docnum,direction,idoctyp,mestyp,status,status_text,created_at FROM idoc"
    where, params = [], []
    if mestyp:
        where.append("mestyp = ?")
        params.append(mestyp.upper())
    if settled:
        where.append("docnum IN (SELECT docnum FROM idoc_statement_line "
                     "WHERE accounting_document = ?)")
        params.append(settled)
    if where:
        sql += " WHERE " + " AND ".join(where)
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
    now = clock.now()
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
            "BELNR": row["SalesOrder"],
            "NTGEW": money.text(row["TotalNetAmount"], 3),
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
            "POSEX": it["SalesOrderItem"],
            "MENGE": money.text(it["RequestedQuantity"], 3),
            "MENEE": it["RequestedQuantityUnit"], "PSTYV": it["SalesOrderItemCategory"],
            "WERKS": it["Plant"], "NTGEW": money.text(it["NetAmount"], 3),
            "CURCY": it["TransactionCurrency"],
        }, child))

    xml = ('<?xml version="1.0" encoding="utf-8"?><ORDERS05><IDOC BEGIN="1">%s</IDOC></ORDERS05>'
           % "".join(segments))

    ctx.conn.execute(
        "INSERT INTO idoc(docnum,direction,idoctyp,mestyp,status,status_text,"
        "created_at,content_type,payload) VALUES(?,?,?,?,?,?,?,?,?)",
        (docnum, "1", "ORDERS05", "ORDERS", "03", STATUS_TEXT["03"],
         clock.stamp(), "xml", xml),
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
         clock.stamp(), "xml", xml),
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
    today = clock.now().strftime("%Y%m%d")
    currency = row["TransactionCurrency"]
    cents = money.CURRENCY_SCALE
    net, tax = invoice["net"], invoice["tax"]

    segments = [
        _control_record(docnum, "INVOIC02", "INVOIC", ctx.client),
        _seg("E1EDK01", {
            "CURCY": currency, "HWAER": currency, "WKURS": "1.00000",
            "ZTERM": row["CustomerPaymentTerms"], "BELNR": billing,
            "NTGEW": money.text(net, 3), "BSART": "INVO",
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
        quantity = money.of(item["RequestedQuantity"])
        amount = money.of(item["NetAmount"])
        price = (money.at(amount / quantity, money.CURRENCY_SCALE)
                 if quantity else amount)
        children = "".join([
            _seg("E1EDP19", {"QUALF": "002", "IDTNR": item["Material"],
                             "KTEXT": item["SalesOrderItemText"]}),
            _seg("E1EDP26", {"QUALF": "003", "BETRG": money.text(amount, cents)}),
            _seg("E1EDP26", {"QUALF": "011", "BETRG": money.text(price, cents)}),
        ])
        segments.append(_seg("E1EDP01", {
            "POSEX": item["SalesOrderItem"], "MENGE": money.text(quantity, 3),
            "MENEE": item["RequestedQuantityUnit"], "PSTYV": item["SalesOrderItemCategory"],
            "WERKS": item["Plant"], "VPREI": money.text(price, cents),
            "NETWR": money.text(amount, cents),
            "CURCY": currency, "VGBEL": row["SalesOrder"], "VGPOS": item["SalesOrderItem"],
        }, children))
    for sumid, total in (("010", net + tax), ("011", net), ("205", tax)):
        segments.append(_seg("E1EDS01", {"SUMID": sumid,
                                         "SUMME": money.text(total, cents),
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
        ctx, row, [(item, money.of(item["RequestedQuantity"])) for item in items])
    docnum = db.next_number(ctx.conn, "IDOC", 16)
    today = clock.now().strftime("%Y%m%d")
    weight = sum((money.of(item["RequestedQuantity"]) for item in items),
                 Decimal(0))

    children = "".join([
        _seg("E1EDL21", {"LFART": "LF", "VSTEL": "1710", "VKORG": row["SalesOrganization"],
                         "ROUTE": "R00001", "BTGEW": money.text(weight, 3),
                         "GEWEI": "KGM"}),
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
        quantity = money.of(item["RequestedQuantity"])
        item_segments.append(_seg("E1EDL24", {
            "POSNR": item["SalesOrderItem"], "MATNR": item["Material"],
            "WERKS": item["Plant"], "LFIMG": money.text(quantity, 3),
            "VRKME": item["RequestedQuantityUnit"],
            "LGMNG": money.text(quantity, 3),
            "MEINS": item["RequestedQuantityUnit"],
            "ARKTX": item["SalesOrderItemText"],
            "VGBEL": row["SalesOrder"], "VGPOS": item["SalesOrderItem"],
        }, _seg("E1EDL18", {"QUALF": "ORI", "PARAM": "SALESORDER"})))

    segments = [
        _control_record(docnum, "DELVRY07", "DELVRY", ctx.client),
        _seg("E1EDL20", {"VBELN": delivery, "VSTEL": "1710", "VKORG": row["SalesOrganization"],
                         "LFART": "LF", "KUNNR": row["SoldToParty"],
                         "BTGEW": money.text(weight, 3), "GEWEI": "KGM",
                         "ANZPK": str(len(items)).zfill(5)},
             children + "".join(item_segments)),
    ]
    xml = ('<?xml version="1.0" encoding="utf-8"?><DELVRY07><IDOC BEGIN="1">%s'
           "</IDOC></DELVRY07>" % "".join(segments))
    _store_idoc(ctx, docnum, "DELVRY07", "DELVRY", xml)
    return {"docnum": docnum, "xml": xml, "delivery": delivery}


def _money_out(amount) -> str:
    """An amount leaving this account, written the way SAP writes a negative.

    The minus goes after the number, and the sign is the whole direction: no
    `EDIF5025` qualifier says debit or credit, which `statement.py` sets out
    for the inbound side. An advice says the same thing the same way, so the
    mock has one convention for both rather than one per direction. A reader
    converting this to an X12 820 maps money out of the payer to `BPR03` `C`,
    a credit on the supplier's account; the perspective flips in the
    conversion, not here.
    """
    return money.text(amount, money.CURRENCY_SCALE) + "-"


def generate_pexr2002(ctx, clearing_document: str) -> dict:
    """Render what a payment document settled as an outbound REMADV (#106).

    The one generator whose source is not a sales order. A remittance advice
    tells a supplier which of their invoices one credit covered, and those
    facts belong to a payment, which is why `generate` asks `source_of` what
    a message type is generated from rather than assuming an order.

    The segments are the payment family's own, the ones `statement.py`
    already reads on a `FINSTA01` for the same kinds of fact: `E1IDKU1` for
    the advice, `E1EDK03` for its date, `E1EDKA1` for who is being paid,
    `E1EDP02` for the number they quote and `E1IDPU5` for an amount. This is
    a subset of what SAP sends and is deliberately short: nothing is emitted
    that the mock does not actually know. There is no `E1IDB02`, because the
    account the money left is not in this mock's data and inventing one would
    be output nothing wrote.

    Both of mock-edi's findings for a payer are answered by construction
    rather than checked afterwards. The total is `settlement_of`'s sum of the
    rows and nothing here recomputes it, so it cannot disagree with its parts.
    The date is the payment's own posting date, so the advice cannot claim a
    settlement the payment has not reached.
    """
    paid = reconcile.settlement_of(ctx, clearing_document)
    docnum = db.next_number(ctx.conn, "IDOC", 16)
    settled = str(paid["settled"] or "").replace("-", "")[:8]

    rows = []
    for row in paid["rows"]:
        # A settled item with no supplier invoice behind it has no number the
        # supplier would recognise. The row carries its amount and no
        # reference, rather than quoting our document as though it were theirs.
        reference = (_seg("E1EDP02", {"QUALF": "009", "BELNR": row["reference"]})
                     if row["reference"] else "")
        rows.append(_seg("E1IDPU1", {}, reference + _seg("E1IDPU5", {
            "MOAQUAL": "001", "MOABETR": _money_out(row["amount"]),
            "CUXWAERZ": paid["currency"]})))

    segments = [
        _control_record(docnum, "PEXR2002", "REMADV", ctx.client),
        _seg("E1IDKU1", {"BGMREF": paid["document"]},
             _seg("E1EDK03", {"IDDAT": "026", "DATUM": settled})
             + _seg("E1EDKA1", {"PARVW": "LF", "LIFNR": paid["payee"]})
             + _seg("E1IDPU5", {"MOAQUAL": "001",
                                "MOABETR": _money_out(paid["total"]),
                                "CUXWAERZ": paid["currency"]})
             + "".join(rows)),
    ]
    xml = ('<?xml version="1.0" encoding="utf-8"?><PEXR2002><IDOC BEGIN="1">%s'
           "</IDOC></PEXR2002>" % "".join(segments))
    _store_idoc(ctx, docnum, "PEXR2002", "REMADV", xml)
    return {"docnum": docnum, "xml": xml,
            "clearing_document": paid["document"],
            "payee": paid["payee"],
            "total": money.text(paid["total"], money.CURRENCY_SCALE),
            "invoices": [row["reference"] or row["invoice"]
                         for row in paid["rows"]]}


# message type -> (basic type, what it is generated *from*, the generator)
GENERATORS = {
    "ORDERS": ("ORDERS05", "SalesOrder",
               lambda ctx, order: generate_orders05(ctx, order)),
    "ORDERS05": ("ORDERS05", "SalesOrder",
                 lambda ctx, order: generate_orders05(ctx, order)),
    "INVOIC": ("INVOIC02", "SalesOrder", generate_invoic02),
    "INVOIC02": ("INVOIC02", "SalesOrder", generate_invoic02),
    "DELVRY": ("DELVRY07", "SalesOrder", generate_delvry07),
    "DELVRY07": ("DELVRY07", "SalesOrder", generate_delvry07),
    "REMADV": ("PEXR2002", "ClearingAccountingDocument", generate_pexr2002),
    "PEXR2002": ("PEXR2002", "ClearingAccountingDocument", generate_pexr2002),
}


def _generator_for(mestyp: str) -> tuple:
    entry = GENERATORS.get((mestyp or "ORDERS").upper())
    if entry is None:
        raise SapError(
            "Message type %s cannot be generated; this mock generates %s"
            % (mestyp, ", ".join(sorted({v[0] for v in GENERATORS.values()}))), 400)
    return entry


def source_of(mestyp: str) -> str:
    """What this message type is generated *from*.

    Three of the four read a sales order; a remittance advice reads a payment
    (#106). The route asks rather than assuming, so a generator with a new
    kind of source does not mean editing the route again.
    """
    return _generator_for(mestyp)[1]


def generate(ctx, mestyp: str, source: str) -> dict:
    """Generate an outbound IDoc of the requested message type."""
    return _generator_for(mestyp)[2](ctx, source)
