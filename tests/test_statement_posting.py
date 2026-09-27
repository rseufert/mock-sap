"""Posting a bank statement: clearing what it paid, reopening what came back.

The other half of #57. `statement.py` says what the file claims; this is what
the claim does to the open items, which is where it can be expensively wrong.
"""
from __future__ import annotations

import unittest

from support import MockServerCase

CUBE = ("/sap/opu/odata/sap/API_OPLACCTGDOCITEMCUBE_SRV"
        "/A_OperationalAcctgDocItemCube")


def invoic(reference, gross="1190.00", net="1000.00", tax="190.00",
           supplier="1000009"):
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


def line(number, amount, reference=None, note=None):
    """One statement line: a trailing minus is money out."""
    parts = ['<E1IDPF1 SEGMENT="1"><LINLINEIT>%s</LINLINEIT>' % number]
    if reference is not None:
        parts.append('<E1EDP02 SEGMENT="1"><QUALF>009</QUALF><BELNR>%s</BELNR>'
                     "</E1EDP02>" % reference)
    if note is not None:
        chunks = [note[i:i + 70] for i in range(0, len(note), 70)] or [""]
        inner = "".join("<TXT%02d>%s</TXT%02d>" % (n, chunk, n)
                        for n, chunk in enumerate(chunks, start=1))
        parts.append('<E1IDT01 SEGMENT="1">%s</E1IDT01>' % inner)
    parts.append('<E1IDPU5 SEGMENT="1"><MOAQUAL>001</MOAQUAL>'
                 "<MOABETR>%s</MOABETR><CUXWAERZ>EUR</CUXWAERZ></E1IDPU5>" % amount)
    parts.append("</E1IDPF1>")
    return "".join(parts)


def balances(opening=None, closing=None, debits=None, credits_=None):
    amounts = []
    for qualifier, value in (("019", opening), ("021", closing),
                             ("023", debits), ("024", credits_)):
        if value is not None:
            amounts.append('<E1IDPU5 SEGMENT="1"><MOAQUAL>%s</MOAQUAL>'
                           "<MOABETR>%s</MOABETR><CUXWAERZ>EUR</CUXWAERZ>"
                           "</E1IDPU5>" % (qualifier, value))
    if not amounts:
        return ""
    return ('<E1IDPF1 SEGMENT="1"><LINLINEIT>000900</LINLINEIT>%s</E1IDPF1>'
            % "".join(amounts))


def finsta(lines="", statement="00042", date="20260927", account="0007000063",
           opening=None, closing=None, debits=None, credits_=None):
    return (
        '<?xml version="1.0" encoding="utf-8"?><FINSTA01><IDOC BEGIN="1">'
        '<EDI_DC40 SEGMENT="1"><IDOCTYP>FINSTA01</IDOCTYP>'
        "<MESTYP>FINSTA</MESTYP></EDI_DC40>"
        '<E1IDKU1 SEGMENT="1"><BGMREF>%s</BGMREF>'
        '<E1EDK03 SEGMENT="1"><IDDAT>026</IDDAT><DATUM>%s</DATUM></E1EDK03>'
        '<E1IDB02 SEGMENT="1"><FIIBKENN>37040044</FIIBKENN>'
        "<FIIKONTO>%s</FIIKONTO><FIIBLAND>DE</FIIBLAND><FIIKWAER>EUR</FIIKWAER>"
        "</E1IDB02>%s%s</E1IDKU1></IDOC></FINSTA01>"
    ) % (statement, date, account, lines,
         balances(opening, closing, debits, credits_))


class StatementCase(MockServerCase):
    def send(self, body):
        headers = dict(self.csrf_token(), **{"Content-Type": "application/xml",
                                             "Accept": "application/json"})
        status, _, receipt = self.request("POST", "/sap/bc/idoc", body=body,
                                          headers=headers)
        self.assertEqual(status, 201)
        return receipt

    def bill(self, reference, gross="1190.00"):
        """A supplier invoice, so there is something open to pay."""
        net = "%.2f" % (float(gross) / 1.19)
        tax = "%.2f" % (float(gross) - float(net))
        return self.send(invoic(reference, gross, net, tax))["APPLIED"][0]

    def open_payables(self):
        _, _, body = self.get(
            CUBE + "?$filter=AccountingDocumentItemType%20eq%20'K'%20and%20"
            "ClearingAccountingDocument%20eq%20''&$format=json")
        return body["d"]["results"]

    def item_of(self, accounting_document):
        _, _, body = self.get(
            CUBE + "?$filter=AccountingDocument%%20eq%%20'%s'%%20and%%20"
            "AccountingDocumentItemType%%20eq%%20'K'&$format=json"
            % accounting_document)
        return body["d"]["results"][0]


class TestClearingWhatItPaid(StatementCase):
    def test_a_statement_clears_exactly_the_items_it_pays(self):
        paid = self.bill("SUP-A1", "1190.00")
        also = self.bill("SUP-A2", "2380.00")
        untouched = self.bill("SUP-A3", "500.00")
        before = {r["AccountingDocument"] for r in self.open_payables()}
        for invoice in (paid, also, untouched):
            self.assertIn(invoice["ACCOUNTINGDOCUMENT"], before)

        applied = self.send(finsta(
            line("000001", "1190.00-", reference="SUP-A1")
            + line("000002", "2380.00-", reference="SUP-A2")))["APPLIED"][0]

        self.assertEqual(len(applied["CLEARED"]), 2)
        self.assertEqual(applied["UNPROCESSED"], [])
        after = {r["AccountingDocument"] for r in self.open_payables()}
        self.assertEqual(before - after,
                         {paid["ACCOUNTINGDOCUMENT"], also["ACCOUNTINGDOCUMENT"]},
                         "exactly those two stopped being open")
        self.assertIn(untouched["ACCOUNTINGDOCUMENT"], after,
                      "and the one nobody paid is still owed")

    def test_a_cleared_item_names_the_document_that_paid_it(self):
        invoice = self.bill("SUP-B1", "1190.00")
        applied = self.send(finsta(
            line("000001", "1190.00-", reference="SUP-B1")))["APPLIED"][0]

        item = self.item_of(invoice["ACCOUNTINGDOCUMENT"])
        self.assertEqual(item["ClearingAccountingDocument"],
                         applied["CLEARED"][0]["CLEARINGDOCUMENT"])
        self.assertTrue(item["ClearingDate"])
        self.assertFalse(item["ClearingIsReversed"])

    def test_the_payment_does_not_leave_a_payable_of_its_own(self):
        """A payment document's own supplier line is not a new debt."""
        invoice = self.bill("SUP-B2", "1190.00")
        before = len(self.open_payables())
        applied = self.send(finsta(
            line("000001", "1190.00-", reference="SUP-B2")))["APPLIED"][0]

        self.assertEqual(len(self.open_payables()), before - 1,
                         "one fewer open item, not one fewer and one more")
        clearing = applied["CLEARED"][0]["CLEARINGDOCUMENT"]
        self.assertNotIn(clearing,
                         [r["AccountingDocument"] for r in self.open_payables()])

    def test_the_invoice_number_only_in_the_note_to_payee(self):
        invoice = self.bill("SUP-C1", "1190.00")
        applied = self.send(finsta(line(
            "000001", "1190.00-",
            note="PAYMENT FOR INVOICE SUP-C1 THANK YOU")))["APPLIED"][0]

        self.assertEqual(len(applied["CLEARED"]), 1)
        self.assertEqual(applied["CLEARED"][0]["ACCOUNTINGDOCUMENT"],
                         invoice["ACCOUNTINGDOCUMENT"])

    def test_a_reference_split_by_the_seventy_character_wrap(self):
        """`SUP-D1` arriving as `SUP- D1` still finds its invoice."""
        invoice = self.bill("SUP-D1", "1190.00")
        note = "X" * 66 + "SUP-D1 PAID"      # the reference straddles TXT01/TXT02
        applied = self.send(finsta(line("000001", "1190.00-",
                                        note=note)))["APPLIED"][0]

        self.assertEqual(len(applied["CLEARED"]), 1, applied["UNPROCESSED"])
        self.assertEqual(applied["CLEARED"][0]["ACCOUNTINGDOCUMENT"],
                         invoice["ACCOUNTINGDOCUMENT"])


class TestWhatItLeavesAlone(StatementCase):
    def test_the_right_reference_and_the_wrong_amount_clears_nothing(self):
        invoice = self.bill("SUP-E1", "1190.00")
        applied = self.send(finsta(
            line("000001", "1000.00-", reference="SUP-E1")))["APPLIED"][0]

        self.assertEqual(applied["CLEARED"], [])
        [unprocessed] = applied["UNPROCESSED"]
        self.assertIn("SUP-E1", unprocessed["REASON"])
        self.assertIn("1190.00", unprocessed["REASON"], "what is owed")
        self.assertIn("1000.00", unprocessed["REASON"], "what was paid")
        self.assertIn(invoice["ACCOUNTINGDOCUMENT"],
                      [r["AccountingDocument"] for r in self.open_payables()],
                      "still owed, in full")

    def test_a_line_that_matches_nothing_is_reported(self):
        applied = self.send(finsta(
            line("000001", "99.00-", reference="NOT-A-THING")))["APPLIED"][0]
        self.assertEqual(applied["CLEARED"], [])
        self.assertIn("no open item quotes this reference",
                      applied["UNPROCESSED"][0]["REASON"])

    def test_a_failed_posting_clears_nothing(self):
        invoice = self.bill("SUP-F1", "1190.00")
        self.request("POST", "/_mock/idoc-posting",
                     body={"mestyp": "FINSTA", "status": "51",
                           "message": "Bank statement could not be posted"})
        self.addCleanup(self.request, "DELETE", "/_mock/idoc-posting")

        receipt = self.send(finsta(line("000001", "1190.00-",
                                        reference="SUP-F1")))
        self.assertEqual(receipt["STATUS"], "51")
        self.assertNotIn("APPLIED", receipt)
        self.assertIn(invoice["ACCOUNTINGDOCUMENT"],
                      [r["AccountingDocument"] for r in self.open_payables()],
                      "an item cleared by an IDoc that did not post would be "
                      "an invoice nobody can find")


class TestAReturnedPayment(StatementCase):
    def test_a_credit_reopens_the_item_it_paid(self):
        invoice = self.bill("SUP-G1", "1190.00")
        self.send(finsta(line("000001", "1190.00-", reference="SUP-G1"),
                         statement="00050"))
        self.assertNotIn(invoice["ACCOUNTINGDOCUMENT"],
                         [r["AccountingDocument"] for r in self.open_payables()])

        applied = self.send(finsta(line("000001", "1190.00", reference="SUP-G1"),
                                   statement="00051"))["APPLIED"][0]

        self.assertEqual(len(applied["REOPENED"]), 1)
        self.assertIn(invoice["ACCOUNTINGDOCUMENT"],
                      [r["AccountingDocument"] for r in self.open_payables()],
                      "owed again, so a payment run will try again")

    def test_paid_and_returned_is_not_the_same_as_never_paid(self):
        returned = self.bill("SUP-G2", "1190.00")
        never = self.bill("SUP-G3", "1190.00")
        self.send(finsta(line("000001", "1190.00-", reference="SUP-G2"),
                         statement="00060"))
        self.send(finsta(line("000001", "1190.00", reference="SUP-G2"),
                         statement="00061"))

        was_returned = self.item_of(returned["ACCOUNTINGDOCUMENT"])
        was_never_paid = self.item_of(never["ACCOUNTINGDOCUMENT"])

        self.assertEqual(was_returned["ClearingAccountingDocument"], "")
        self.assertEqual(was_never_paid["ClearingAccountingDocument"], "")
        self.assertTrue(was_returned["ClearingIsReversed"],
                        "this one was paid and the payment came back")
        self.assertFalse(was_never_paid["ClearingIsReversed"],
                         "this one nobody ever paid - a treasury team needs "
                         "to tell these apart")


class TestWhatTheStatementSaysAboutItself(StatementCase):
    def test_a_statement_that_does_not_add_up_is_flagged(self):
        applied = self.send(finsta(
            line("000001", "100.00-"), opening="1000.00", closing="5000.00",
            debits="100.00", credits_="0.00"))["APPLIED"][0]

        self.assertTrue(applied["FINDINGS"])
        self.assertIn("does not add up", applied["FINDINGS"][0])
        self.assertIn("900.00", applied["FINDINGS"][0], "what it should close at")

    def test_a_missing_statement_is_flagged(self):
        self.send(finsta(statement="00070", opening="1000.00", closing="2000.00",
                         debits="0.00", credits_="1000.00"))
        applied = self.send(finsta(
            statement="00072", opening="3000.00", closing="3000.00",
            debits="0.00", credits_="0.00"))["APPLIED"][0]

        self.assertTrue(applied["FINDINGS"])
        self.assertIn("a statement is missing", applied["FINDINGS"][0])
        self.assertIn("00070", applied["FINDINGS"][0])

    def test_a_statement_that_follows_the_last_one_is_not_flagged(self):
        self.send(finsta(statement="00080", opening="1000.00", closing="2000.00",
                         debits="0.00", credits_="1000.00"))
        applied = self.send(finsta(
            statement="00081", opening="2000.00", closing="2000.00",
            debits="0.00", credits_="0.00"))["APPLIED"][0]
        self.assertEqual(applied["FINDINGS"], [])


if __name__ == "__main__":
    unittest.main()
