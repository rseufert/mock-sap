"""Integration tests for invoice_check, against mock-sap and mock-edi.

    pip install mock-edi
    python3 -m mocksap --port 8000 &
    mock-edi --port 8080 &
    cd examples && python3 -m unittest -v test_invoice_check
"""
import datetime
import json
import os
import re
import unittest
import urllib.parse
import urllib.request
from decimal import Decimal

from invoice_check import PO_SERVICE, InvoiceCheck, Sap, send_order

SAP = os.environ.get("SAP_URL", "http://127.0.0.1:8000")
EDI = os.environ.get("EDI_URL", "http://127.0.0.1:8080")


def sap_date(raw):
    """OData V2's `/Date(1788220800000)/`, as a date."""
    found = re.search(r"-?\d+", raw or "")
    return None if not found else (
        datetime.datetime(1970, 1, 1)
        + datetime.timedelta(milliseconds=int(found.group()))).date()


def control(base, method, path, body=None):
    """Talk to a mock's /_mock control plane."""
    req = urllib.request.Request(base + path, method=method,
                                 data=json.dumps(body).encode() if body else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as response:
        return json.loads(response.read() or "null")


class SupplierInvoices(unittest.TestCase):

    def setUp(self):
        control(SAP, "POST", "/_mock/reset")
        control(EDI, "POST", "/_mock/reset")
        self.sap = Sap(SAP)
        self.check = InvoiceCheck(self.sap, EDI, our_id="ACME")

    def order(self, widget_price="12.50"):
        """A PO in SAP for 100 widgets and 40 brackets, sent to the supplier."""
        po = self.sap.request("POST", PO_SERVICE + "/A_PurchaseOrder", {
            "PurchaseOrderType": "NB", "CompanyCode": "1710",
            "PurchasingOrganization": "1710", "PurchasingGroup": "001",
            "Supplier": "1000012", "DocumentCurrency": "USD",
            "to_PurchaseOrderItem": [
                {"Material": "TG11", "OrderQuantity": "100", "NetPriceAmount": widget_price,
                 "PurchaseOrderQuantityUnit": "PC", "Plant": "1010"},
                {"Material": "TG12", "OrderQuantity": "40", "NetPriceAmount": "4.15",
                 "PurchaseOrderQuantityUnit": "PC", "Plant": "1010"},
            ]})["d"]["PurchaseOrder"]
        summary = send_order(self.sap, EDI, po, sender="ACME")
        self.assertTrue(summary["accepted"])
        return po

    def supplier_behaves(self, behaviour):
        control(EDI, "PATCH", "/_mock/partners/ACME", {"behaviour": behaviour})

    def invoice_idocs(self):
        return control(SAP, "GET", "/_mock/idocs?mestyp=INVOIC")["results"]

    def supplier_invoices(self, reference):
        q = urllib.parse.urlencode({
            "$filter": "SupplierInvoiceIDByInvcgParty eq '%s'" % reference,
            "$format": "json"})
        return self.sap.request(
            "GET", "/sap/opu/odata/sap/API_SUPPLIERINVOICE_PROCESS_SRV"
                   "/A_SupplierInvoice?" + q)["d"]["results"]

    def invoice_items(self, supplier_invoice):
        q = urllib.parse.urlencode({
            "$filter": "SupplierInvoice eq '%s'" % supplier_invoice,
            "$format": "json"})
        return self.sap.request(
            "GET", "/sap/opu/odata/sap/API_SUPPLIERINVOICE_PROCESS_SRV"
                   "/A_SuplrInvcItemPurOrdRef?" + q)["d"]["results"]

    def open_items(self):
        """What a payment run would select: open supplier items."""
        q = urllib.parse.urlencode({
            "$filter": "AccountingDocumentItemType eq 'K' and "
                       "ClearingAccountingDocument eq ''",
            "$format": "json"})
        return self.sap.request(
            "GET", "/sap/opu/odata/sap/API_OPLACCTGDOCITEMCUBE_SRV"
                   "/A_OperationalAcctgDocItemCube?" + q)["d"]["results"]

    def test_matching_invoice_is_posted(self):
        po = self.order()

        [result] = self.check.run()
        self.assertEqual((result["po"], result["status"], result["problems"]),
                         (po, "posted", []))
        [idoc] = self.invoice_idocs()
        self.assertEqual((idoc["docnum"], idoc["status"]), (result["idoc"], "53"))

    def test_a_posted_invoice_leaves_money_owed(self):
        """The assertion whose absence let this example post nothing for a year.

        Every test here asserted what SAP *received* - the IDoc and its status -
        and none asserted what posting it created. So when the IDoc stopped
        naming the supplier, SAP created no supplier invoice and no payable,
        answered status 53 anyway, and five green tests said it was fine. A
        payment run then had nothing to pay: the invoice was approved, posted,
        and owed to nobody (mock-sap#67, #68).
        """
        po = self.order()

        [result] = self.check.run()
        self.assertEqual(result["status"], "posted")
        self.assertTrue(result["supplier_invoice"], "posting it created nothing")

        invoices = self.supplier_invoices(result["invoice"])
        self.assertEqual(len(invoices), 1, invoices)
        self.assertEqual(invoices[0]["SupplierInvoiceIDByInvcgParty"],
                         result["invoice"],
                         "a payment run matches the bank's answer on this")
        self.assertEqual(invoices[0]["InvoicingParty"], "1000012")

        items = self.open_items()
        self.assertEqual(len(items), 1, items)
        self.assertEqual(items[0]["Supplier"], "1000012")
        # 100 widgets at 12.50 and 40 brackets at 4.15, as the 810 billed.
        self.assertEqual(abs(Decimal(items[0]["AmountInTransactionCurrency"])),
                         Decimal("1416.00"))
        self.assertEqual(items[0]["TransactionCurrency"], "USD")

    def test_the_payable_lines_carry_the_amounts_and_the_order(self):
        """A payable whose total is right and whose lines are worth nothing is
        still wrong: three-way matching in SAP happens per item, against the
        purchase order line. NETWR and VGBEL/VGPOS on each E1EDP01 are what put
        them there.
        """
        po = self.order()

        [result] = self.check.run()
        items = self.invoice_items(result["supplier_invoice"])

        self.assertEqual(len(items), 2, items)
        by_amount = sorted(Decimal(i["SupplierInvoiceItemAmount"]) for i in items)
        # 100 widgets at 12.50, and 40 brackets at 4.15.
        self.assertEqual(by_amount, [Decimal("166.00"), Decimal("1250.00")])
        self.assertEqual({i["PurchaseOrder"] for i in items}, {po})

    def test_the_payable_falls_due_on_the_invoices_own_terms(self):
        """Net 30 reaches the payable, so a payment run picks it up when due.

        The 810 carries its net days in ITD07 (`ITD*08*3*2**10**30`) and its
        date in BIG01. Posting with neither left the payable due immediately,
        which is a different invoice from the one the supplier sent.

        What this cannot show is that the baseline is the *invoice's* date
        rather than the posting date: mock-edi bills on the current date, so the
        two are the same here and an assertion would prove nothing. The date is
        read from BIG01 and sent as E1EDK03/026 - see invoic_idoc - and a test
        that could tell them apart needs a supplier that backdates.
        """
        po = self.order()

        [result] = self.check.run()
        self.assertEqual(result["status"], "posted")

        [item] = self.open_items()
        [invoice] = self.supplier_invoices(result["invoice"])
        baseline = sap_date(invoice["DueCalculationBaseDate"])
        self.assertEqual(sap_date(item["NetDueDate"]),
                         baseline + datetime.timedelta(days=30))
        self.assertEqual(invoice["PaymentTerms"], "NT30")

    def test_short_shipment_billed_as_shipped_is_posted(self):
        # Checking the invoice against the PO quantity would block this one.
        # The supplier shipped 80 and 32 and billed 80 and 32: that is correct.
        self.supplier_behaves("short-ship")
        self.order()

        [result] = self.check.run()
        self.assertEqual(result["status"], "posted")

    def test_price_disagreement_is_blocked(self):
        # We ordered widgets at 11.00; the supplier's catalogue says 12.50,
        # and a real supplier bills its own price.
        self.order(widget_price="11.00")

        [result] = self.check.run()
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["problems"], ["item 00010 billed at 12.50, ordered at 11.00"])
        self.assertEqual(self.invoice_idocs(), [])

    def test_an_idoc_sap_would_not_post_is_not_treated_as_posted(self):
        # SAP answers 201 with a docnum and then declines to post the invoice.
        # Reading the docnum and stopping there books an invoice SAP rejected.
        control(SAP, "POST", "/_mock/idoc-posting",
                {"mestyp": "INVOIC", "status": "51",
                 "message": "Posting period 08 2026 is not open"})
        self.order()

        [result] = self.check.run()
        self.assertEqual(result["status"], "not posted")
        self.assertIn("status 51", result["problems"][0])

        # SAP's own record agrees, and the invoice is not marked as posted here,
        # so the resend after someone opens the period is not a duplicate
        [idoc] = self.invoice_idocs()
        self.assertEqual(idoc["status"], "51")
        self.assertEqual(self.check.posted, set())

        control(SAP, "DELETE", "/_mock/idoc-posting")

    def test_duplicate_invoice_is_posted_once(self):
        # A supplier with a retry bug sends the same invoice again a second
        # later.  Release it now rather than sleeping.
        self.supplier_behaves("duplicate-invoice")
        self.order()

        [first] = self.check.run()
        control(EDI, "POST", "/_mock/advance?all")
        [second] = self.check.run()

        self.assertEqual(first["invoice"], second["invoice"])
        self.assertEqual(first["status"], "posted")
        self.assertEqual(second["status"], "blocked")
        self.assertIn("already been posted", second["problems"][0])
        self.assertEqual(len(self.invoice_idocs()), 1)

if __name__ == "__main__":
    unittest.main()
