"""invoice_check: verify a supplier's EDI invoices against SAP before posting.

The three-way match every accounts-payable integration does: an 810 invoice
is posted into SAP (as an inbound INVOIC IDoc) only if

    - its prices agree with the purchase order in SAP,
    - it bills no more than the supplier's 856 ship notice said was shipped,
    - its total adds up, and
    - it has not been posted before.

An IDoc SAP accepted is not an invoice SAP posted, so the status record that
comes back decides: only status 53 counts as posted.

Anything else is blocked with the reasons, for a person to look at.

The supplier is mock-edi (https://github.com/rseufert/mock-edi), which answers
an 850 with a 997, an 855, an 856 and an 810, and misbehaves on request.  The
tests are in test_invoice_check.py.  mock-edi's examples/po_bridge.py is the
other half of the same integration: purchase orders out, confirmations in.
"""
import datetime
import http.cookiejar
import json
import urllib.request
from decimal import Decimal

PO_SERVICE = "/sap/opu/odata/sap/API_PURCHASEORDER_PROCESS_SRV"

# SAP material -> the supplier's part number (the purchasing info record)
SUPPLIER_PART = {"TG11": "WIDGET-001", "TG12": "BRKT-050", "TG14": "GEAR-100"}


class Sap:
    """Just enough of an OData/IDoc client: a session and a CSRF token."""

    def __init__(self, base):
        self.base = base
        jar = http.cookiejar.CookieJar()
        self.http = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        self.token = None

    def request(self, method, path, body=None, content_type="application/json"):
        headers = {"Accept": "application/json"}
        if method != "GET":
            if self.token is None:
                self.token = self._fetch_token()
            headers["X-CSRF-Token"] = self.token
            headers["Content-Type"] = content_type
        if isinstance(body, (dict, list)):
            body = json.dumps(body)
        data = body.encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data, headers, method=method)
        with self.http.open(req) as response:
            return json.loads(response.read())

    def _fetch_token(self):
        req = urllib.request.Request(self.base + PO_SERVICE + "/",
                                     headers={"X-CSRF-Token": "Fetch"})
        with self.http.open(req) as response:
            return response.headers["X-CSRF-Token"]

    def purchase_order(self, number):
        path = "%s/A_PurchaseOrder('%s')?$expand=to_PurchaseOrderItem" % (PO_SERVICE, number)
        return self.request("GET", path)["d"]


def edi(base, method, path, body=None):
    req = urllib.request.Request(base + path, method=method,
                                 data=body.encode() if body else None,
                                 headers={"Content-Type": "application/edi-x12"})
    with urllib.request.urlopen(req) as response:
        return json.loads(response.read())


def send_order(sap, edi_base, po_number, sender, receiver="MOCKEDI", control=1):
    """Send an SAP purchase order to the supplier as an 850.

    The minimum needed to set up an invoice; mock-edi's examples/po_bridge.py
    does this properly and brings the 855 back.
    """
    po = sap.purchase_order(po_number)
    items = po["to_PurchaseOrderItem"]["results"]
    now = datetime.datetime.now(datetime.timezone.utc)
    body = ["ST*850*0001", "BEG*00*SA*%s**%s" % (po_number, now.strftime("%Y%m%d"))]
    body += ["PO1*%s*%d*EA*%s**VP*%s" % (i["PurchaseOrderItem"], float(i["OrderQuantity"]),
                                          i["NetPriceAmount"], SUPPLIER_PART[i["Material"]])
             for i in items]
    body += ["CTT*%d" % len(items)]
    body += ["SE*%d*0001" % (len(body) + 1)]
    envelope = [
        "ISA*00*%-10s*00*%-10s*ZZ*%-15s*ZZ*%-15s*%s*%s*U*00401*%09d*0*T*>" % (
            "", "", sender, receiver, now.strftime("%y%m%d"), now.strftime("%H%M"), control),
        "GS*PO*%s*%s*%s*%s*%d*X*004010" % (
            sender, receiver, now.strftime("%Y%m%d"), now.strftime("%H%M"), control)]
    trailer = ["GE*1*%d" % control, "IEA*1*%09d" % control]
    return edi(edi_base, "POST", "/edi", "~\n".join(envelope + body + trailer) + "~\n")


def segments(payload):
    for segment in payload.replace("\n", "").split("~"):
        if segment:
            yield segment.split("*")


def read_856(payload):
    """The ship notice as (po_number, {sap_item: quantity shipped})."""
    po_number, shipped = "", {}
    for fields in segments(payload):
        if fields[0] == "PRF":
            po_number = fields[1]
        elif fields[0] == "SN1":
            shipped[fields[1]] = Decimal(fields[2])
    return po_number, shipped


def read_810(payload):
    """The invoice as a dict: number, po, lines {item: (qty, price)}, total."""
    invoice = {"lines": {}}
    for fields in segments(payload):
        if fields[0] == "BIG":
            invoice["number"], invoice["po"] = fields[2], fields[4]
        elif fields[0] == "IT1":
            invoice["lines"][fields[1]] = (Decimal(fields[2]), Decimal(fields[4]))
        elif fields[0] == "TDS":
            invoice["total"] = Decimal(fields[1]) / 100     # two implied decimals
    return invoice


def invoic_idoc(invoice):
    """The invoice as the INVOIC02 IDoc SAP's invoice verification reads."""
    items = "".join(
        "<E1EDP01><POSEX>%s</POSEX><MENGE>%s</MENGE><VPREI>%s</VPREI>"
        "<E1EDP02><QUALF>001</QUALF><BELNR>%s</BELNR><ZEILE>%s</ZEILE></E1EDP02></E1EDP01>"
        % (item, qty, price, invoice["po"], item)
        for item, (qty, price) in sorted(invoice["lines"].items()))
    return ("<INVOIC02><IDOC BEGIN=\"1\"><EDI_DC40 SEGMENT=\"1\">"
            "<IDOCTYP>INVOIC02</IDOCTYP><MESTYP>INVOIC</MESTYP><DIRECT>2</DIRECT>"
            "</EDI_DC40><E1EDK01 SEGMENT=\"1\"><BELNR>%s</BELNR></E1EDK01>"
            "<E1EDK02><QUALF>001</QUALF><BELNR>%s</BELNR></E1EDK02>%s"
            "<E1EDS01><SUMID>010</SUMID><SUMME>%s</SUMME></E1EDS01>"
            "</IDOC></INVOIC02>" % (invoice["number"], invoice["po"], items, invoice["total"]))


class InvoiceCheck:
    def __init__(self, sap, edi_base, our_id):
        self.sap, self.edi_base, self.our_id = sap, edi_base, our_id
        self.shipped = {}       # po_number -> {item: quantity}, from 856s
        self.posted = set()     # invoice numbers already in SAP

    def problems(self, invoice):
        """Why this invoice must not be posted; empty if it may be."""
        if invoice["number"] in self.posted:
            return ["invoice %s has already been posted" % invoice["number"]]
        po = self.sap.purchase_order(invoice["po"])
        ordered = {i["PurchaseOrderItem"]: i for i in po["to_PurchaseOrderItem"]["results"]}
        shipped = self.shipped.get(invoice["po"], {})
        found, total = [], Decimal(0)
        for item, (qty, price) in sorted(invoice["lines"].items()):
            total += qty * price
            if item not in ordered:
                found.append("item %s is not on purchase order %s" % (item, invoice["po"]))
                continue
            po_price = Decimal(ordered[item]["NetPriceAmount"])
            if price != po_price:
                found.append("item %s billed at %s, ordered at %s" % (item, price, po_price))
            if qty > shipped.get(item, 0):
                found.append("item %s bills %s, shipped %s" % (item, qty, shipped.get(item, 0)))
        if total != invoice["total"]:
            found.append("lines add up to %s, invoice total is %s" % (total, invoice["total"]))
        return found

    def run(self):
        """Collect ship notices and invoices; post what matches, block the rest."""
        docs = [d for d in edi(self.edi_base, "GET", "/_mock/mailbox?partner=%s" % self.our_id)
                if d["code"] in ("856", "810")]
        for doc in docs:                       # ship notices first: invoices need them
            if doc["code"] == "856":
                po_number, lines = read_856(doc["payload"])
                self.shipped.setdefault(po_number, {}).update(lines)
        results = []
        for doc in docs:
            if doc["code"] != "810":
                continue
            invoice = read_810(doc["payload"])
            result = {"invoice": invoice["number"], "po": invoice["po"],
                      "problems": self.problems(invoice)}
            if result["problems"]:
                result["status"] = "blocked"
            else:
                receipt = self.sap.request("POST", "/sap/bc/idoc",
                                           invoic_idoc(invoice), "application/xml")
                # A 201 means SAP took the IDoc, not that it posted the invoice.
                # The status record says which, and only 53 is posted; treating
                # the docnum as success books an invoice SAP rejected, and marks
                # the number as posted so the resend looks like a duplicate.
                if receipt.get("STATUS") == "53":
                    self.posted.add(invoice["number"])
                    result.update(status="posted", idoc=receipt["DOCNUM"])
                else:
                    result.update(status="not posted", idoc=receipt["DOCNUM"],
                                  problems=["IDoc %s is in status %s: %s"
                                            % (receipt["DOCNUM"], receipt.get("STATUS"),
                                               receipt.get("STATUS_TEXT", ""))])
            results.append(result)
        return results
