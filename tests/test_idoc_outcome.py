"""What posting a DELVRY or an INVOIC decided, read back from the IDoc (#116).

#105 did this for the bank statement. These are the other two message types
that post something, and they dropped their answer the same way: the receipt
carried it once and a later read of the IDoc returned nine columns, none of
them this.

The assertion that matters in most of these is equality with the receipt, not
a field-by-field check. An outcome that reads back nearly right - a missing
`DELIVERY`, a sentence rebuilt with the wrong words - is worse than one that is
plainly absent, because a client has no way to tell it is being lied to.

Every test makes its own sales order: a class shares one database, and a
delivery against an order a sibling test already delivered would assert
something that depends on the alphabet.
"""
from __future__ import annotations

import unittest

from support import MockServerCase, SRV

ORDERS05 = ('<?xml version="1.0" encoding="utf-8"?><ORDERS05><IDOC BEGIN="1">'
            '<EDI_DC40 SEGMENT="1"><IDOCTYP>ORDERS05</IDOCTYP>'
            "<MESTYP>ORDERS</MESTYP></EDI_DC40></IDOC></ORDERS05>")


def delvry(*orders, announced="0080007777"):
    """A DELVRY07 delivering four of item 000010 of each order named."""
    items = "".join(
        '<E1EDL24 SEGMENT="1"><POSNR>000010</POSNR><MATNR>TG11</MATNR>'
        "<LFIMG>4.000</LFIMG><VGBEL>%s</VGBEL><VGPOS>000010</VGPOS></E1EDL24>"
        % order for order in orders)
    return (
        '<?xml version="1.0" encoding="utf-8"?><DELVRY07><IDOC BEGIN="1">'
        '<EDI_DC40 SEGMENT="1"><IDOCTYP>DELVRY07</IDOCTYP><MESTYP>DELVRY</MESTYP>'
        "</EDI_DC40>"
        '<E1EDL20 SEGMENT="1"><VBELN>%s</VBELN>%s</E1EDL20>'
        "</IDOC></DELVRY07>") % (announced, items)


def invoic(reference, gross="1190.00", supplier="1000009"):
    net = "%.2f" % (float(gross) / 1.19)
    tax = "%.2f" % (float(gross) - float(net))
    return (
        '<?xml version="1.0" encoding="utf-8"?><INVOIC02><IDOC BEGIN="1">'
        '<EDI_DC40 SEGMENT="1"><IDOCTYP>INVOIC02</IDOCTYP>'
        "<MESTYP>INVOIC</MESTYP></EDI_DC40>"
        '<E1EDK01 SEGMENT="1"><CURCY>EUR</CURCY><ZTERM>NT30</ZTERM>'
        "<BELNR>%s</BELNR></E1EDK01>"
        '<E1EDK02 SEGMENT="1"><QUALF>009</QUALF><BELNR>%s</BELNR></E1EDK02>'
        '<E1EDK03 SEGMENT="1"><IDDAT>026</IDDAT><DATUM>20260927</DATUM></E1EDK03>'
        '<E1EDKA1 SEGMENT="1"><PARVW>LF</PARVW><LIFNR>%s</LIFNR></E1EDKA1>'
        '<E1EDP01 SEGMENT="1"><POSEX>000010</POSEX><MENGE>1.000</MENGE>'
        "<MENEE>PC</MENEE><NETWR>%s</NETWR><VGBEL>4500000100</VGBEL>"
        "<VGPOS>00010</VGPOS></E1EDP01>"
        '<E1EDS01 SEGMENT="1"><SUMID>010</SUMID><SUMME>%s</SUMME></E1EDS01>'
        '<E1EDS01 SEGMENT="1"><SUMID>011</SUMID><SUMME>%s</SUMME></E1EDS01>'
        '<E1EDS01 SEGMENT="1"><SUMID>205</SUMID><SUMME>%s</SUMME></E1EDS01>'
        "</IDOC></INVOIC02>") % (reference, reference, supplier, net, gross,
                                 net, tax)


class OutcomeCase(MockServerCase):
    def send(self, body, expect=201):
        headers = dict(self.csrf_token(), **{"Content-Type": "application/xml",
                                             "Accept": "application/json"})
        status, _, receipt = self.request("POST", "/sap/bc/idoc", body=body,
                                          headers=headers)
        self.assertEqual(status, expect)
        return receipt

    def idoc(self, docnum):
        _, _, body = self.get("/sap/bc/idoc/%s" % docnum)
        return body

    def an_order(self, quantity="4"):
        """A sales order nobody has delivered yet."""
        _, _, created = self.request(
            "POST", SRV + "/A_SalesOrder", headers=self.csrf_token(), body={
                "SalesOrderType": "OR", "SalesOrganization": "1710",
                "SoldToParty": "1000001", "DistributionChannel": "10",
                "OrganizationDivision": "00",
                "to_Item": [{"Material": "TG11", "RequestedQuantity": quantity,
                             "RequestedQuantityUnit": "PC", "NetAmount": "400"}]})
        self.assertEqual(created["d"]["OverallDeliveryStatus"], "A")
        return created["d"]["SalesOrder"]


class TestADeliveryRemembersWhatItMoved(OutcomeCase):
    def test_the_outcome_reads_back_as_the_receipt_said_it(self):
        receipt = self.send(delvry(self.an_order()))

        self.assertEqual(self.idoc(receipt["DOCNUM"])["APPLIED"],
                         receipt["APPLIED"],
                         "the same answer, not one recomputed to look like it")

    def test_the_delivery_it_created_is_named_on_the_read_too(self):
        receipt = self.send(delvry(self.an_order()))
        applied = receipt["APPLIED"][0]
        self.assertIn("DELIVERY", applied, "the announced delivery was unknown")

        read = self.idoc(receipt["DOCNUM"])["APPLIED"][0]
        self.assertEqual(read["DELIVERY"], applied["DELIVERY"])
        self.assertIn("created", read["MESSAGE"])

    def test_a_partial_delivery_still_says_partly_after_a_read(self):
        receipt = self.send(delvry(self.an_order(quantity="10")))
        self.assertEqual(receipt["APPLIED"][0]["STATUS"], "B")

        read = self.idoc(receipt["DOCNUM"])["APPLIED"][0]
        self.assertEqual(read["STATUS"], "B")
        self.assertIn("partly delivered", read["MESSAGE"])

    def test_an_order_the_mock_could_not_find_is_remembered_as_missing(self):
        """The refusal #116 argued for: nothing else records that it happened."""
        receipt = self.send(delvry("9999999999"))

        read = self.idoc(receipt["DOCNUM"])["APPLIED"][0]
        self.assertEqual(read["STATUS"], "")
        self.assertEqual(read["MESSAGE"],
                         "Sales order 9999999999 does not exist")
        self.assertNotIn("DELIVERY", read,
                         "an order that does not exist got no delivery")

    def test_two_orders_in_one_idoc_read_back_in_the_order_they_moved(self):
        first, second = self.an_order(), self.an_order()
        receipt = self.send(delvry(first, second))
        self.assertEqual(len(receipt["APPLIED"]), 2)

        read = self.idoc(receipt["DOCNUM"])["APPLIED"]
        self.assertEqual(read, receipt["APPLIED"])
        self.assertEqual([row["SALESORDER"] for row in read], [first, second])

    def test_one_that_moved_and_one_that_does_not_exist_keep_their_own_answers(self):
        order = self.an_order()
        receipt = self.send(delvry(order, "9999999999"))

        read = self.idoc(receipt["DOCNUM"])["APPLIED"]
        self.assertEqual(read, receipt["APPLIED"])
        moved = [row for row in read if row["SALESORDER"] == order][0]
        missing = [row for row in read if row["SALESORDER"] == "9999999999"][0]
        self.assertEqual(moved["STATUS"], "C")
        self.assertEqual(missing["STATUS"], "")


class TestAnInvoiceRemembersWhatItPosted(OutcomeCase):
    def test_the_outcome_reads_back_as_the_receipt_said_it(self):
        receipt = self.send(invoic("OWN-A1"))

        self.assertEqual(self.idoc(receipt["DOCNUM"])["APPLIED"],
                         receipt["APPLIED"])

    def test_the_accounting_document_is_the_number_a_payment_quotes(self):
        """#116's reason for persisting this one: it is reachable, not free."""
        receipt = self.send(invoic("OWN-B1"))
        posted = receipt["APPLIED"][0]

        read = self.idoc(receipt["DOCNUM"])["APPLIED"][0]
        self.assertEqual(read["ACCOUNTINGDOCUMENT"],
                         posted["ACCOUNTINGDOCUMENT"])
        self.assertEqual(read["SUPPLIERINVOICE"], posted["SUPPLIERINVOICE"])
        self.assertEqual(read["FISCALYEAR"], posted["FISCALYEAR"])

        _, _, item = self.get(
            "/sap/opu/odata/sap/API_OPLACCTGDOCITEMCUBE_SRV"
            "/A_OperationalAcctgDocItemCube?$filter=AccountingDocument%%20eq%%20"
            "'%s'%%20and%%20AccountingDocumentItemType%%20eq%%20'K'&$format=json"
            % read["ACCOUNTINGDOCUMENT"])
        self.assertEqual(len(item["d"]["results"]), 1,
                         "the document the read names is a real payable")

    def test_the_sentence_still_names_the_money_after_a_read(self):
        """Which is only possible because the amount was kept, not the sentence."""
        receipt = self.send(invoic("OWN-C1", gross="2380.00"))

        read = self.idoc(receipt["DOCNUM"])["APPLIED"][0]
        self.assertIn("EUR 2380.00 payable to 1000009", read["MESSAGE"])
        self.assertEqual(read["MESSAGE"], receipt["APPLIED"][0]["MESSAGE"])


class TestAnIDocThatDecidedNothing(OutcomeCase):
    def test_an_orders_idoc_has_no_outcome_at_all(self):
        receipt = self.send(ORDERS05)
        self.assertNotIn("APPLIED", receipt, "an ORDERS05 is only filed")

        self.assertNotIn("APPLIED", self.idoc(receipt["DOCNUM"]),
                         "absent, not an empty list that reads as an answer")

    def test_a_posting_the_application_declined_files_nothing(self):
        """51 means it did not happen, so there is no outcome to find."""
        status, _, _ = self.request(
            "POST", "/_mock/idoc-posting",
            body={"mestyp": "DELVRY", "status": "51",
                  "message": "Plant 1710 is closed for goods issue"})
        self.assertEqual(status, 201)
        self.addCleanup(self.request, "DELETE", "/_mock/idoc-posting")

        receipt = self.send(delvry(self.an_order()))
        self.assertEqual(receipt["STATUS"], "51")
        self.assertNotIn("APPLIED", receipt)

        self.assertNotIn("APPLIED", self.idoc(receipt["DOCNUM"]),
                         "an IDoc that posted nothing moved nothing")

    def test_a_delivery_refused_before_it_moved_anything_files_nothing(self):
        """NotPosted is raised mid-application; nothing may survive it."""
        nothing = ('<?xml version="1.0" encoding="utf-8"?><DELVRY07>'
                   '<IDOC BEGIN="1"><EDI_DC40 SEGMENT="1">'
                   "<IDOCTYP>DELVRY07</IDOCTYP><MESTYP>DELVRY</MESTYP>"
                   "</EDI_DC40></IDOC></DELVRY07>")
        receipt = self.send(nothing)
        self.assertEqual(receipt["STATUS"], "51")

        self.assertNotIn("APPLIED", self.idoc(receipt["DOCNUM"]))


class TestEachKindReadsBackItsOwnShape(OutcomeCase):
    """`outcome.of` asks the tables, not the message type. One row, one answer."""

    def test_three_postings_do_not_borrow_each_other_s_outcomes(self):
        delivery = self.send(delvry(self.an_order()))
        invoice = self.send(invoic("MIX-A1"))

        read_delivery = self.idoc(delivery["DOCNUM"])["APPLIED"]
        read_invoice = self.idoc(invoice["DOCNUM"])["APPLIED"]

        self.assertEqual(len(read_delivery), 1)
        self.assertEqual(len(read_invoice), 1)
        self.assertEqual(sorted(read_delivery[0]),
                         ["DELIVERY", "MESSAGE", "SALESORDER", "STATUS"])
        self.assertEqual(sorted(read_invoice[0]),
                         ["ACCOUNTINGDOCUMENT", "FISCALYEAR", "INVOICINGPARTY",
                          "MESSAGE", "SUPPLIERINVOICE"])


if __name__ == "__main__":
    unittest.main()
