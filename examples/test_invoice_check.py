"""Integration tests for invoice_check, against mock-sap and mock-edi.

    pip install mock-edi
    python3 -m mocksap --port 8000 &
    mock-edi --port 8080 &
    cd examples && python3 -m unittest -v test_invoice_check
"""
import json
import os
import unittest
import urllib.request

from invoice_check import PO_SERVICE, InvoiceCheck, Sap, send_order

SAP = os.environ.get("SAP_URL", "http://127.0.0.1:8000")
EDI = os.environ.get("EDI_URL", "http://127.0.0.1:8080")


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

    def test_matching_invoice_is_posted(self):
        po = self.order()

        [result] = self.check.run()
        self.assertEqual((result["po"], result["status"], result["problems"]),
                         (po, "posted", []))
        [idoc] = self.invoice_idocs()
        self.assertEqual((idoc["docnum"], idoc["status"]), (result["idoc"], "53"))

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
