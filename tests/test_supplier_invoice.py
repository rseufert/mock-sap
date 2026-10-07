"""A supplier's invoice: filed, posted, and owed until something pays it.

The half of the document chain that runs the other way. A sales order becomes
money owed to us; a supplier's INVOIC becomes money owed by us, and that
payable is what a payment run selects, pays and clears.
"""
from __future__ import annotations

import unittest
from decimal import Decimal

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

    def test_an_invoice_with_no_invoicing_party_is_not_posted(self):
        """51, not 53, and the text says what was missing.

        Status 53 is *Application document posted*. Reporting it for an IDoc
        that posted nothing tells a client the opposite of what happened, and
        leaves it nothing to check: a client that reads the status rather than
        trusting the 201 - the whole lesson of /_mock/idoc-posting - is still
        told the invoice posted. That the IDoc *arrived* is the 201 and the
        document number, both asserted here, and is not what 53 says.
        """
        before = len(self.invoices())
        body = invoic().replace("<PARVW>LF</PARVW>", "<PARVW>XX</PARVW>")
        receipt = self.send(body=body)

        self.assertEqual(receipt["STATUS"], "51")
        self.assertIn("E1EDKA1", receipt["STATUS_TEXT"], receipt["STATUS_TEXT"])
        self.assertTrue(receipt["DOCNUM"], "the IDoc was still received and filed")
        self.assertNotIn("APPLIED", receipt)
        self.assertEqual(len(self.invoices()), before)

    def test_the_filed_idoc_carries_the_status_that_happened(self):
        """Not just the receipt: the IDoc in the database says 51 too.

        The status used to be decided before the application ran, so what was
        filed could disagree with what happened. Anyone reading the IDoc back -
        which is how a person looks at this in SAP - would see "posted".
        """
        body = invoic(reference="SUP-NOPARTY").replace(
            "<PARVW>LF</PARVW>", "<PARVW>XX</PARVW>")
        receipt = self.send(body=body)

        _, _, listing = self.get("/sap/bc/idoc?mestyp=INVOIC")
        filed = [row for row in listing["results"]
                 if row["docnum"] == receipt["DOCNUM"]]
        self.assertEqual(len(filed), 1, listing["results"])
        self.assertEqual(filed[0]["status"], "51")
        self.assertIn("E1EDKA1", filed[0]["status_text"])

    def test_an_invoice_with_no_total_is_not_posted(self):
        before = len(self.invoices())
        body = invoic().replace("<SUMID>010</SUMID><SUMME>1190.00</SUMME>",
                                "<SUMID>010</SUMID><SUMME></SUMME>")
        receipt = self.send(body=body)

        self.assertEqual(receipt["STATUS"], "51")
        self.assertIn("SUMID 010", receipt["STATUS_TEXT"], receipt["STATUS_TEXT"])
        self.assertEqual(len(self.invoices()), before)

    def documents_posted(self):
        status, _, count = self.get(
            "/sap/opu/odata/sap/API_JOURNALENTRY_SRV/A_JournalEntry/$count",
            raw=True)
        self.assertEqual(status, 200)
        return int(count)

    def lines_of(self, document):
        _, _, body = self.get(
            CUBE + "?$filter=AccountingDocument%%20eq%%20'%s'&$format=json"
            % document)
        return body["d"]["results"]

    def invoiced_as(self, reference):
        return [row for row in self.invoices()
                if row["SupplierInvoiceIDByInvcgParty"] == reference]

    def test_a_total_that_is_not_net_plus_tax_is_not_posted(self):
        """51, and nothing written: an unbalanced document is not a document.

        1000.00 net and 190.00 tax are 1190.00. An INVOIC that says 1200.00
        would credit the supplier ten more than it debits anything, and FI
        refuses that before it refuses anything else (#93).
        """
        invoices, documents = len(self.invoices()), self.documents_posted()
        receipt = self.send(reference="SUP-OFF-BY-TEN", gross="1200.00")

        self.assertEqual(receipt["STATUS"], "51", receipt)
        self.assertNotIn("APPLIED", receipt)
        for said in ("1200.00", "1000.00", "190.00", "10.00", "SUMID 010"):
            self.assertIn(said, receipt["STATUS_TEXT"])
        self.assertEqual(len(self.invoices()), invoices)
        self.assertEqual(self.documents_posted(), documents,
                         "no half of an accounting document was left behind")
        self.assertEqual(self.invoiced_as("SUP-OFF-BY-TEN"), [])

    def test_items_that_do_not_add_up_to_the_net_are_not_posted(self):
        """The header agrees with itself and the items disagree with it."""
        invoices, documents = len(self.invoices()), self.documents_posted()
        body = invoic(reference="SUP-ITEMS-SHORT").replace(
            "<NETWR>1000.00</NETWR>", "<NETWR>900.00</NETWR>")
        receipt = self.send(body=body)

        self.assertEqual(receipt["STATUS"], "51", receipt)
        for said in ("900.00", "1000.00", "100.00", "NETWR"):
            self.assertIn(said, receipt["STATUS_TEXT"])
        self.assertEqual(len(self.invoices()), invoices)
        self.assertEqual(self.documents_posted(), documents)

    def test_the_unbalanced_idoc_is_filed_as_51(self):
        receipt = self.send(reference="SUP-FILED-51", tax="19.00")

        _, _, listing = self.get("/sap/bc/idoc?mestyp=INVOIC")
        [filed] = [row for row in listing["results"]
                   if row["docnum"] == receipt["DOCNUM"]]
        self.assertEqual(filed["status"], "51")
        self.assertIn("Balance not zero", filed["status_text"])

    def test_a_total_and_a_tax_with_no_net_still_balance(self):
        """What the IDoc leaves out is worked out, not assumed.

        With no SUMID 011 the net used to be taken for the gross, so 1190.00
        with 190.00 of tax debited 1380.00 against a credit of 1190.00.
        """
        body = invoic(reference="SUP-NO-NET").replace(
            '<E1EDS01 SEGMENT="1"><SUMID>011</SUMID><SUMME>1000.00</SUMME>'
            "</E1EDS01>", "")
        receipt = self.send(body=body)

        self.assertEqual(receipt["STATUS"], "53", receipt)
        document = receipt["APPLIED"][0]["ACCOUNTINGDOCUMENT"]
        booked = sorted((line["DebitCreditCode"],
                         Decimal(line["AmountInTransactionCurrency"]))
                        for line in self.lines_of(document))
        self.assertEqual(booked, [("H", Decimal("1190.00")),
                                  ("S", Decimal("190.00")),
                                  ("S", Decimal("1000.00"))])

    def test_a_total_alone_posts_as_it_always_did(self):
        """Only SUMID 010, and items that say nothing: no claim to check."""
        body = (invoic(reference="SUP-TOTAL-ONLY")
                .replace('<E1EDS01 SEGMENT="1"><SUMID>011</SUMID>'
                         "<SUMME>1000.00</SUMME></E1EDS01>", "")
                .replace('<E1EDS01 SEGMENT="1"><SUMID>205</SUMID>'
                         "<SUMME>190.00</SUMME></E1EDS01>", "")
                .replace("<NETWR>1000.00</NETWR>", ""))
        self.assertNotIn("011", body)
        receipt = self.send(body=body)

        self.assertEqual(receipt["STATUS"], "53", receipt)
        booked = sorted((line["DebitCreditCode"],
                         Decimal(line["AmountInTransactionCurrency"]))
                        for line in self.lines_of(
                            receipt["APPLIED"][0]["ACCOUNTINGDOCUMENT"]))
        self.assertEqual(booked, [("H", Decimal("1190.00")),
                                  ("S", Decimal("1190.00"))])

    def test_a_well_formed_invoice_still_posts(self):
        """The regression that matters: none of the above changed the good path."""
        receipt = self.send(reference="SUP-STILL-GOOD")

        self.assertEqual(receipt["STATUS"], "53")
        self.assertEqual(receipt["STATUS_TEXT"], "Application document posted")
        self.assertEqual(len(receipt["APPLIED"]), 1)
        self.assertTrue(receipt["APPLIED"][0]["SUPPLIERINVOICE"])

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




class TestPostingOneThroughOData(SupplierInvoiceCase):
    """A POST posts the invoice, as an inbound INVOIC does (#97, #154).

    It used to land a row and nothing else: no accounting document, no open
    item, nothing to say it was not posted, and a 201. An invoice that exists
    and can never be paid.
    """

    def post(self, **fields):
        body = dict({"CompanyCode": "1710", "InvoicingParty": "1000009",
                     "SupplierInvoiceIDByInvcgParty": "ODATA-1",
                     "InvoiceGrossAmount": "1190.00",
                     "DocumentCurrency": "EUR", "PaymentTerms": "NT30"},
                    **fields)
        return self.request("POST", SRV + "/A_SupplierInvoice?$format=json",
                            headers=self.csrf_token(), body=body)

    def test_it_posts_an_accounting_document_and_an_open_payable(self):
        status, _, created = self.post()
        self.assertEqual(status, 201)
        invoice = created["d"]
        self.assertEqual(invoice["SupplierInvoiceStatus"], "5", "posted")
        self.assertTrue(invoice["AccountingDocument"],
                        "and it names the document it posted")

        # checked against the cube, not against the invoice's own say-so
        _, _, body = self.get(
            CUBE + "?$filter=AccountingDocument%%20eq%%20'%s'&$format=json"
            % invoice["AccountingDocument"])
        payables = [r for r in body["d"]["results"]
                    if r["AccountingDocumentItemType"] == "K"]
        self.assertEqual(len(payables), 1)
        payable = payables[0]
        self.assertEqual(payable["Supplier"], "1000009")
        self.assertEqual(Decimal(payable["AmountInTransactionCurrency"]),
                         Decimal("1190.00"))
        self.assertEqual(payable["DebitCreditCode"], "H", "we owe it")
        self.assertEqual(payable["ClearingAccountingDocument"], "",
                         "open until something pays it")
        self.assertTrue(payable["NetDueDate"], "and it falls due")

    def test_the_number_comes_from_the_range_so_the_next_invoic_still_posts(self):
        """#154: the POST used to take the number the range would give next.

        It numbered from `MAX(existing) + 1`, so the following inbound INVOIC
        drew a number that was already there and could not post.
        """
        first = self.send(reference="SUP-BEFORE")["APPLIED"][0]["SUPPLIERINVOICE"]
        status, _, created = self.post(SupplierInvoiceIDByInvcgParty="ODATA-2")
        self.assertEqual(status, 201)
        through_odata = created["d"]["SupplierInvoice"]
        self.assertNotEqual(through_odata, first)

        receipt = self.send(reference="SUP-AFTER")
        self.assertEqual(receipt["STATUS"], "53",
                         receipt.get("STATUS_TEXT", ""))
        third = receipt["APPLIED"][0]["SUPPLIERINVOICE"]
        self.assertEqual(len({first, through_odata, third}), 3,
                         "three invoices, three numbers")

    def test_the_items_it_bills_against_are_recorded(self):
        status, _, created = self.post(
            SupplierInvoiceIDByInvcgParty="ODATA-3",
            to_SuplrInvcItemPurOrdRef=[{
                "PurchaseOrder": "4500000100", "PurchaseOrderItem": "00010",
                "SupplierInvoiceItemAmount": "1190.00",
                "QuantityInPurchaseOrderUnit": "10",
                "PurchaseOrderQuantityUnit": "PC",
                "SupplierInvoiceItemText": "Widgets"}])
        self.assertEqual(status, 201)
        invoice = created["d"]
        _, _, body = self.get(
            SRV + "/A_SupplierInvoice(SupplierInvoice='%s',FiscalYear='%s')"
            "/to_SuplrInvcItemPurOrdRef?$format=json"
            % (invoice["SupplierInvoice"], invoice["FiscalYear"]))
        items = body["d"]["results"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["PurchaseOrder"], "4500000100")
        self.assertEqual(Decimal(items[0]["SupplierInvoiceItemAmount"]),
                         Decimal("1190.00"))

    def test_items_that_do_not_come_to_the_total_are_not_posted(self):
        """`post_supplier_invoice`'s own rule, inherited rather than repeated."""
        before = len(self.invoices())
        status, _, body = self.post(
            SupplierInvoiceIDByInvcgParty="ODATA-4",
            to_SuplrInvcItemPurOrdRef=[{
                "PurchaseOrder": "4500000100", "PurchaseOrderItem": "00010",
                "SupplierInvoiceItemAmount": "900.00"}])
        self.assertEqual(status, 400)
        self.assertIn("items come to", body["error"]["message"]["value"])
        self.assertEqual(len(self.invoices()), before, "nothing was written")

    def test_what_it_refuses(self):
        before = len(self.invoices())

        # the document number is the range's to give, not the client's
        status, _, body = self.post(SupplierInvoice="5100009999")
        self.assertEqual(status, 400)
        self.assertIn("SupplierInvoice", body["error"]["message"]["value"])

        # and a field this route does not act on is named, not dropped
        status, _, body = self.post(AccountingDocument="0100000001")
        self.assertEqual(status, 400)
        message = body["error"]["message"]["value"]
        self.assertIn("AccountingDocument", message)
        self.assertIn("InvoicingParty", message, "and it says what it does take")

        status, _, body = self.post(InvoicingParty="")
        self.assertEqual(status, 400)

        self.assertEqual(len(self.invoices()), before, "none of them wrote")


class TestBlockingAnInvoice(SupplierInvoiceCase):
    """An invoice and the item that owes the money are one decision.

    Blocking the invoice and leaving its open item payable is the worst of
    both worlds: the invoice reads as blocked and the payment goes out anyway.
    Reported by mock-bank against 0.12.0 (#62).
    """

    def payable_of(self, accounting_document):
        _, _, body = self.get(
            CUBE + "?$filter=AccountingDocument%%20eq%%20'%s'%%20and%%20"
            "AccountingDocumentItemType%%20eq%%20'K'&$format=json"
            % accounting_document)
        return body["d"]["results"][0]

    def test_blocking_the_invoice_blocks_its_open_item(self):
        applied = self.send()["APPLIED"][0]
        self.assertEqual(self.payable_of(applied["ACCOUNTINGDOCUMENT"])
                         ["PaymentBlockingReason"], "")

        status, _, _ = self.request(
            "PATCH", SRV + "/A_SupplierInvoice(SupplierInvoice='%s',FiscalYear='%s')"
            % (applied["SUPPLIERINVOICE"], applied["FISCALYEAR"]),
            body={"PaymentBlockingReason": "A"},
            headers=dict(self.csrf_token(), **{"If-Match": "*"}))
        self.assertEqual(status, 204)

        self.assertEqual(self.payable_of(applied["ACCOUNTINGDOCUMENT"])
                         ["PaymentBlockingReason"], "A",
                         "a payment run reads the item, not the invoice")

    def test_a_blocked_item_drops_out_of_the_payment_run_selection(self):
        applied = self.send(reference="SUP-BLOCK")["APPLIED"][0]
        selection = (CUBE + "?$filter=AccountingDocumentItemType%20eq%20'K'%20and%20"
                     "ClearingAccountingDocument%20eq%20''%20and%20"
                     "PaymentBlockingReason%20eq%20''&$format=json")

        _, _, before = self.get(selection)
        self.assertIn(applied["ACCOUNTINGDOCUMENT"],
                      [r["AccountingDocument"] for r in before["d"]["results"]])

        self.request(
            "PATCH", SRV + "/A_SupplierInvoice(SupplierInvoice='%s',FiscalYear='%s')"
            % (applied["SUPPLIERINVOICE"], applied["FISCALYEAR"]),
            body={"PaymentBlockingReason": "A"},
            headers=dict(self.csrf_token(), **{"If-Match": "*"}))

        _, _, after = self.get(selection)
        self.assertNotIn(applied["ACCOUNTINGDOCUMENT"],
                         [r["AccountingDocument"] for r in after["d"]["results"]],
                         "blocked means a payment run does not pick it up")

    def test_unblocking_puts_it_back(self):
        applied = self.send(reference="SUP-UNBLOCK")["APPLIED"][0]
        key = (SRV + "/A_SupplierInvoice(SupplierInvoice='%s',FiscalYear='%s')"
               % (applied["SUPPLIERINVOICE"], applied["FISCALYEAR"]))
        headers = dict(self.csrf_token(), **{"If-Match": "*"})

        self.request("PATCH", key, body={"PaymentBlockingReason": "A"},
                     headers=headers)
        self.request("PATCH", key, body={"PaymentBlockingReason": ""},
                     headers=headers)

        self.assertEqual(self.payable_of(applied["ACCOUNTINGDOCUMENT"])
                         ["PaymentBlockingReason"], "",
                         "and it is payable again")

    def test_the_payment_method_is_blank_by_default(self):
        """Blank is what SAP leaves when the vendor master decides.

        Confirmed with mock-bank rather than guessed: their example reads a
        blank method and `T` as a transfer, and skips anything else.
        """
        applied = self.send(reference="SUP-METHOD")["APPLIED"][0]
        _, _, body = self.get(
            SRV + "/A_SupplierInvoice(SupplierInvoice='%s',FiscalYear='%s')"
            "?$format=json" % (applied["SUPPLIERINVOICE"], applied["FISCALYEAR"]))
        self.assertEqual(body["d"]["PaymentMethod"], "")


class TestClaimingAnInvoiceForAPaymentRun(SupplierInvoiceCase):
    """The state between open and cleared, so a second run can see it (#90).

    A run selected open items, paid them, and the fact that it had paid them
    lived only in its own memory until the statement came back days later. A
    second run started in between selected the same invoice and paid it
    again - one 1190.00 invoice paid 2380.00 - and the bank's duplicate check
    could not catch it, being keyed on a `MsgId` the second run made fresh.

    `PaymentRunID` and `PaymentRunDate` are F110's own key for a run, and
    saying which run holds an item is what a payment block cannot do: a block
    is one character and means nobody should pay this at all.
    """

    SELECTION = (CUBE + "?$filter=AccountingDocumentItemType%20eq%20'K'%20and%20"
                 "ClearingAccountingDocument%20eq%20''%20and%20"
                 "PaymentBlockingReason%20eq%20''%20and%20"
                 "PaymentRunID%20eq%20''&$format=json")

    def payable_of(self, accounting_document):
        _, _, body = self.get(
            CUBE + "?$filter=AccountingDocument%%20eq%%20'%s'%%20and%%20"
            "AccountingDocumentItemType%%20eq%%20'K'&$format=json"
            % accounting_document)
        return body["d"]["results"][0]

    def claim(self, applied, run="F110A", date="2026-10-05"):
        body = {"PaymentRunID": run}
        if date is not None:
            body["PaymentRunDate"] = date
        return self.request(
            "PATCH", SRV + "/A_SupplierInvoice(SupplierInvoice='%s',FiscalYear='%s')"
            % (applied["SUPPLIERINVOICE"], applied["FISCALYEAR"]),
            body=body, headers=dict(self.csrf_token(), **{"If-Match": "*"}))

    def selected(self):
        _, _, body = self.get(self.SELECTION)
        return [r["AccountingDocument"] for r in body["d"]["results"]]

    def test_an_invoice_posts_with_no_run_on_it(self):
        """Nothing has selected it yet, and that is a state it can be in."""
        applied = self.send(reference="SUP-RUN0")["APPLIED"][0]
        item = self.payable_of(applied["ACCOUNTINGDOCUMENT"])
        self.assertEqual(item["PaymentRunID"], "")
        self.assertIsNone(item["PaymentRunDate"])

    def test_claiming_the_invoice_claims_its_open_item(self):
        """A run writes it on the invoice; the next run reads the item."""
        applied = self.send(reference="SUP-RUN1")["APPLIED"][0]

        status, _, _ = self.claim(applied, run="F110A")
        self.assertEqual(status, 204)

        item = self.payable_of(applied["ACCOUNTINGDOCUMENT"])
        self.assertEqual(item["PaymentRunID"], "F110A",
                         "a payment run reads the item, not the invoice")
        self.assertTrue(item["PaymentRunDate"],
                        "and the run date travelled with it")

    def test_a_claimed_item_drops_out_of_the_payment_run_selection(self):
        """The fault itself: the second run no longer sees it."""
        applied = self.send(reference="SUP-RUN2")["APPLIED"][0]
        document = applied["ACCOUNTINGDOCUMENT"]
        self.assertIn(document, self.selected(), "the first run selects it")

        self.claim(applied)

        self.assertNotIn(document, self.selected(),
                         "a second run started before the statement comes "
                         "back does not pay it again")

    def test_a_claim_is_not_a_block(self):
        """Two different states, and a run has to be able to tell them apart.

        A blocked item is one nobody should pay. A claimed item is one being
        paid right now. Reporting the second as the first would tell a
        treasury team to go and unblock something that is simply in flight.
        """
        applied = self.send(reference="SUP-RUN3")["APPLIED"][0]
        self.claim(applied)

        item = self.payable_of(applied["ACCOUNTINGDOCUMENT"])
        self.assertEqual(item["PaymentBlockingReason"], "",
                         "nothing blocked it")
        self.assertEqual(item["ClearingAccountingDocument"], "",
                         "and nothing has cleared it either - it is open, and "
                         "in flight")

    def test_releasing_the_claim_puts_it_back(self):
        """A run that was cancelled has to be able to let go."""
        applied = self.send(reference="SUP-RUN4")["APPLIED"][0]
        document = applied["ACCOUNTINGDOCUMENT"]
        self.claim(applied)
        self.assertNotIn(document, self.selected())

        status, _, _ = self.request(
            "PATCH", SRV + "/A_SupplierInvoice(SupplierInvoice='%s',FiscalYear='%s')"
            % (applied["SUPPLIERINVOICE"], applied["FISCALYEAR"]),
            body={"PaymentRunID": "", "PaymentRunDate": None},
            headers=dict(self.csrf_token(), **{"If-Match": "*"}))
        self.assertEqual(status, 204)

        self.assertIn(document, self.selected(), "payable again")

    def test_a_run_identification_too_long_is_refused_not_truncated(self):
        """`LAUFI` is six characters. A seventh is a different run.

        Truncating would make two runs look like one, which is the fault this
        field exists to prevent rather than a formatting detail.
        """
        applied = self.send(reference="SUP-RUN5")["APPLIED"][0]

        status, _, body = self.claim(applied, run="F110-ABC")

        self.assertEqual(status, 400, body)
        self.assertEqual(self.payable_of(applied["ACCOUNTINGDOCUMENT"])
                         ["PaymentRunID"], "", "and nothing was claimed")


if __name__ == "__main__":
    unittest.main()
