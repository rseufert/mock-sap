"""A supplier's invoice: filed, posted, and owed until something pays it.

The half of the document chain that runs the other way. A sales order becomes
money owed to us; a supplier's INVOIC becomes money owed by us, and that
payable is what a payment run selects, pays and clears.
"""
from __future__ import annotations

import unittest

from support import MockServerCase

SRV = "/sap/opu/odata/sap/API_SUPPLIERINVOICE_PROCESS_SRV"
CUBE = ("/sap/opu/odata/sap/API_OPLACCTGDOCITEMCUBE_SRV"
        "/A_OperationalAcctgDocItemCube")


def invoic(reference="SUP-2026-0001", supplier="1000009", gross="1190.00",
           net="1000.00", tax="190.00", terms="NT30", date="20260926",
           purchase_order="4500000100", mestyp="INVOIC"):
    return (
        '<?xml version="1.0" encoding="utf-8"?><INVOIC02><IDOC BEGIN="1">'
        '<EDI_DC40 SEGMENT="1"><IDOCTYP>INVOIC02</IDOCTYP>'
        "<MESTYP>%s</MESTYP></EDI_DC40>"
        '<E1EDK01 SEGMENT="1"><CURCY>EUR</CURCY><ZTERM>%s</ZTERM>'
        "<BELNR>%s</BELNR><BSART>INVO</BSART></E1EDK01>"
        '<E1EDK02 SEGMENT="1"><QUALF>009</QUALF><BELNR>%s</BELNR></E1EDK02>'
        '<E1EDK03 SEGMENT="1"><IDDAT>026</IDDAT><DATUM>%s</DATUM></E1EDK03>'
        '<E1EDKA1 SEGMENT="1"><PARVW>LF</PARVW><PARTN>%s</PARTN>'
        "<LIFNR>%s</LIFNR></E1EDKA1>"
        '<E1EDP01 SEGMENT="1"><POSEX>000010</POSEX><MENGE>10.000</MENGE>'
        "<MENEE>PC</MENEE><NETWR>%s</NETWR><VGBEL>%s</VGBEL>"
        "<VGPOS>00010</VGPOS></E1EDP01>"
        '<E1EDS01 SEGMENT="1"><SUMID>010</SUMID><SUMME>%s</SUMME></E1EDS01>'
        '<E1EDS01 SEGMENT="1"><SUMID>011</SUMID><SUMME>%s</SUMME></E1EDS01>'
        '<E1EDS01 SEGMENT="1"><SUMID>205</SUMID><SUMME>%s</SUMME></E1EDS01>'
        "</IDOC></INVOIC02>"
    ) % (mestyp, terms, reference, reference, date, supplier, supplier,
         net, purchase_order, gross, net, tax)


class SupplierInvoiceCase(MockServerCase):
    def send(self, body=None, **kw):
        headers = dict(self.csrf_token(), **{"Content-Type": "application/xml",
                                             "Accept": "application/json"})
        status, _, receipt = self.request(
            "POST", "/sap/bc/idoc", body=body if body is not None else invoic(**kw),
            headers=headers)
        self.assertEqual(status, 201)
        return receipt

    def invoices(self):
        _, _, body = self.get(SRV + "/A_SupplierInvoice?$format=json")
        return body["d"]["results"]


class TestAnInvoiceBecomesAPayable(SupplierInvoiceCase):
    def test_posting_an_invoic_creates_one_invoice(self):
        before = len(self.invoices())
        receipt = self.send()

        self.assertEqual(receipt["STATUS"], "53")
        self.assertEqual(len(self.invoices()), before + 1,
                         "counted, not read back from the answer")
        applied = receipt["APPLIED"][0]
        self.assertRegex(applied["SUPPLIERINVOICE"], r"^\d{10}$")
        self.assertEqual(applied["INVOICINGPARTY"], "1000009")

    def test_the_invoice_says_what_the_idoc_said(self):
        applied = self.send(reference="SUP-777", gross="2380.00", net="2000.00",
                            tax="380.00", terms="NT60")["APPLIED"][0]
        _, _, body = self.get(
            SRV + "/A_SupplierInvoice(SupplierInvoice='%s',FiscalYear='%s')"
            "?$format=json" % (applied["SUPPLIERINVOICE"], applied["FISCALYEAR"]))
        invoice = body["d"]

        self.assertEqual(invoice["SupplierInvoiceIDByInvcgParty"], "SUP-777")
        self.assertEqual(invoice["InvoicingParty"], "1000009")
        self.assertEqual(invoice["InvoiceGrossAmount"], "2380.00")
        self.assertEqual(invoice["DocumentCurrency"], "EUR")
        self.assertEqual(invoice["PaymentTerms"], "NT60")
        self.assertEqual(invoice["NetPaymentDays"], 60)
        self.assertEqual(invoice["SupplierInvoiceStatus"], "5", "RBSTAT: posted")

    def test_it_leaves_an_open_payable_behind(self):
        applied = self.send(gross="1190.00")["APPLIED"][0]

        _, _, body = self.get(
            CUBE + "?$filter=AccountingDocument%%20eq%%20'%s'&$format=json"
            % applied["ACCOUNTINGDOCUMENT"])
        payables = [r for r in body["d"]["results"]
                    if r["AccountingDocumentItemType"] == "K"]

        self.assertEqual(len(payables), 1)
        payable = payables[0]
        self.assertEqual(payable["Supplier"], "1000009")
        self.assertEqual(payable["AmountInTransactionCurrency"], "1190.000")
        self.assertEqual(payable["DebitCreditCode"], "H", "we owe it")
        self.assertEqual(payable["ClearingAccountingDocument"], "",
                         "open until something pays it")
        self.assertTrue(payable["NetDueDate"], "and it has a date it falls due")

    def test_the_entry_balances(self):
        applied = self.send(gross="1190.00", net="1000.00", tax="190.00")["APPLIED"][0]
        _, _, body = self.get(
            CUBE + "?$filter=AccountingDocument%%20eq%%20'%s'&$format=json"
            % applied["ACCOUNTINGDOCUMENT"])
        rows = body["d"]["results"]

        debits = sum(float(r["AmountInTransactionCurrency"])
                     for r in rows if r["DebitCreditCode"] == "S")
        credits = sum(float(r["AmountInTransactionCurrency"])
                      for r in rows if r["DebitCreditCode"] == "H")
        self.assertEqual(debits, credits, "an accounting document balances")
        self.assertEqual(credits, 1190.00)

    def test_the_purchase_order_it_bills_against_is_recorded(self):
        applied = self.send(purchase_order="4500000123")["APPLIED"][0]
        _, _, body = self.get(
            SRV + "/A_SupplierInvoice(SupplierInvoice='%s',FiscalYear='%s')"
            "?$expand=to_SuplrInvcItemPurOrdRef&$format=json"
            % (applied["SUPPLIERINVOICE"], applied["FISCALYEAR"]))
        items = body["d"]["to_SuplrInvcItemPurOrdRef"]["results"]

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["PurchaseOrder"], "4500000123")
        self.assertEqual(items[0]["PurchaseOrderItem"], "00010")
        self.assertEqual(items[0]["SupplierInvoiceItemAmount"], "1000.00")


class TestWhatDoesNotPost(SupplierInvoiceCase):
    def test_a_failed_posting_creates_nothing(self):
        self.request("POST", "/_mock/idoc-posting",
                     body={"mestyp": "INVOIC", "status": "51",
                           "message": "Posting period 08 2026 is not open"})
        self.addCleanup(self.request, "DELETE", "/_mock/idoc-posting")
        before = len(self.invoices())

        receipt = self.send()

        self.assertEqual(receipt["STATUS"], "51")
        self.assertNotIn("APPLIED", receipt)
        self.assertEqual(len(self.invoices()), before,
                         "an IDoc that did not post owes nobody anything")

    def test_an_invoice_with_no_invoicing_party_posts_nothing(self):
        before = len(self.invoices())
        body = invoic().replace("<PARVW>LF</PARVW>", "<PARVW>XX</PARVW>")
        receipt = self.send(body=body)

        self.assertEqual(receipt["STATUS"], "53", "the IDoc was still received")
        self.assertNotIn("APPLIED", receipt)
        self.assertEqual(len(self.invoices()), before)

    def test_an_invoice_with_no_total_posts_nothing(self):
        before = len(self.invoices())
        body = invoic().replace("<SUMID>010</SUMID><SUMME>1190.00</SUMME>",
                                "<SUMID>010</SUMID><SUMME></SUMME>")
        self.send(body=body)
        self.assertEqual(len(self.invoices()), before)

    def test_the_same_invoice_twice_creates_two(self):
        """SAP's duplicate check is configuration, not arithmetic.

        Inventing one here would hide the bug an accounts-payable integration
        most needs to find, so the mock files both and says so.
        """
        before = len(self.invoices())
        first = self.send(reference="SUP-DUP")["APPLIED"][0]
        second = self.send(reference="SUP-DUP")["APPLIED"][0]

        self.assertNotEqual(first["SUPPLIERINVOICE"], second["SUPPLIERINVOICE"])
        self.assertEqual(len(self.invoices()), before + 2)
        _, _, body = self.get(
            SRV + "/A_SupplierInvoice?$filter=SupplierInvoiceIDByInvcgParty"
            "%20eq%20'SUP-DUP'&$format=json")
        self.assertEqual(len(body["d"]["results"]), 2,
                         "two invoices, both quoting one supplier number")


if __name__ == "__main__":
    unittest.main()
