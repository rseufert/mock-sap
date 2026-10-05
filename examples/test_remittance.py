"""Integration tests for remittance, against mock-sap and mock-edi.

    pip install mock-edi
    python3 -m mocksap --port 8000 &
    mock-edi --port 8080 &
    cd examples && python3 -m unittest -v test_remittance

The point of these is not that the 820 is well formed. It is that the supplier
*reads* it and agrees with it - and that when it should not agree, it says so.
A clean acceptance only means something next to a test that makes the same
reader refuse.
"""
import datetime
import json
import os
import random
import unittest
import urllib.error
import urllib.request
from decimal import Decimal

from invoice_check import Sap
from remittance import (NotMoneyOut, advice_820, read_pexr2002, send,
                        tell_the_supplier)

SAP = os.environ.get("SAP_URL", "http://127.0.0.1:8000")
EDI = os.environ.get("EDI_URL", "http://127.0.0.1:8080")
US = "ACME"            # the X12 trading partner these examples act as

STATEMENT_DATE = "20260927"

# An interchange control number may not repeat for a partner: a second one is
# refused with a TA1 rather than acknowledged with a 997. CI starts a fresh
# mock for every run, so a fixed base would pass there and fail the second time
# anybody ran this locally. The base is drawn per run instead, and the invoice
# references and statement numbers are derived from it so that a rerun against
# a mock that is still holding the last one does not collide either.
RUN = random.randrange(100000, 990000)


def control(base, method, path, body=None):
    req = urllib.request.Request(base + path, method=method,
                                 data=json.dumps(body).encode() if body else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as response:
        return json.loads(response.read() or "null")


def invoic(reference, gross, supplier="1000009"):
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


def finsta(lines, statement, date=STATEMENT_DATE):
    return (
        '<?xml version="1.0" encoding="utf-8"?><FINSTA01><IDOC BEGIN="1">'
        '<EDI_DC40 SEGMENT="1"><IDOCTYP>FINSTA01</IDOCTYP>'
        "<MESTYP>FINSTA</MESTYP></EDI_DC40>"
        '<E1IDKU1 SEGMENT="1"><BGMREF>%s</BGMREF>'
        '<E1EDK03 SEGMENT="1"><IDDAT>026</IDDAT><DATUM>%s</DATUM></E1EDK03>'
        '<E1IDB02 SEGMENT="1"><FIIBKENN>37040044</FIIBKENN>'
        "<FIIKONTO>0007000063</FIIKONTO><FIIBLAND>DE</FIIBLAND>"
        "<FIIKWAER>EUR</FIIKWAER></E1IDB02>%s</E1IDKU1></IDOC></FINSTA01>"
    ) % (statement, date, lines)


def paid(number, amount, reference):
    return ('<E1IDPF1 SEGMENT="1"><LINLINEIT>%s</LINLINEIT>'
            '<E1EDP02 SEGMENT="1"><QUALF>009</QUALF><BELNR>%s</BELNR></E1EDP02>'
            '<E1IDPU5 SEGMENT="1"><MOAQUAL>001</MOAQUAL><MOABETR>%s-</MOABETR>'
            "<CUXWAERZ>EUR</CUXWAERZ></E1IDPU5></E1IDPF1>"
            % (number, reference, amount))


class TellingTheSupplier(unittest.TestCase):
    """One payment, two invoices, and what the supplier makes of the advice."""

    next_control = RUN

    @classmethod
    def setUpClass(cls):
        cls.sap = Sap(SAP)

    def setUp(self):
        TellingTheSupplier.next_control += 1
        self.control = TellingTheSupplier.next_control
        self.statement = str(self.control)

    # -- helpers

    def post_idoc(self, body):
        return self.sap.request("POST", "/sap/bc/idoc", body,
                                content_type="application/xml")

    def a_payment_of(self, *invoices, **kwargs):
        """Bill these, pay them with one statement, return the clearing."""
        date = kwargs.pop("date", STATEMENT_DATE)
        tag = "REM%d" % self.control
        lines = ""
        for n, (suffix, gross) in enumerate(invoices, start=1):
            reference = "%s-%s" % (tag, suffix)
            self.post_idoc(invoic(reference, gross))
            lines += paid("%06d" % n, gross, reference)
        applied = self.post_idoc(
            finsta(lines, self.statement, date))["APPLIED"][0]
        self.assertEqual(len(applied["CLEARED"]), len(invoices),
                         applied["UNPROCESSED"])
        documents = {row["CLEARINGDOCUMENT"] for row in applied["CLEARED"]}
        self.assertEqual(len(documents), 1, "one supplier, one payment")
        return documents.pop(), tag

    def tell(self, clearing_document, **kwargs):
        return tell_the_supplier(self.sap, EDI, clearing_document, US,
                                 control=self.control, **kwargs)

    def only_message(self, receipt):
        sets = receipt["transactionSets"]
        self.assertEqual(len(sets), 1, sets)
        return sets[0]

    # -- tests

    def test_the_supplier_accepts_the_advice_and_agrees_with_it(self):
        clearing, tag = self.a_payment_of(("A1", "1190.00"), ("A2", "2380.00"))

        out = self.tell(clearing)

        message = self.only_message(out["receipt"])
        self.assertEqual(message["code"], "820")
        self.assertTrue(message["accepted"], message["findings"])
        self.assertEqual(message["findings"], [], "nothing wrong with its syntax")
        self.assertEqual(message["disagreements"], [],
                         "and nothing wrong with its business")

    def test_the_total_that_survives_the_conversion_is_the_sum_of_the_rows(self):
        clearing, tag = self.a_payment_of(("B1", "1190.00"), ("B2", "2380.00"))

        out = self.tell(clearing)

        self.assertEqual(out["advice"]["total"], Decimal("3570.00"))
        self.assertEqual(
            sum(row["amount"] for row in out["advice"]["invoices"]),
            out["advice"]["total"],
            "SAP's own total agrees with its rows before anything converts it")
        self.assertIn("BPR*I*3570.00*C*NON", out["interchange"])
        self.assertIn("RMR*IV*%s-B1**1190.00" % tag, out["interchange"])
        self.assertIn("RMR*IV*%s-B2**2380.00" % tag, out["interchange"])

    def test_the_supplier_lists_the_advice_against_the_payment(self):
        clearing, _ = self.a_payment_of(("C1", "1190.00"))

        out = self.tell(clearing)

        listed = [row for row in control(EDI, "GET", "/_mock/remittances")
                  if row["trace"] == clearing]
        self.assertEqual(len(listed), 1, "the advice is filed under the payment")
        self.assertEqual(listed[0]["total"], "1190.00")
        self.assertEqual(listed[0]["creditDebit"], "C",
                         "money out of our account is a credit on theirs")
        self.assertEqual(listed[0]["settles"], "2026-09-27")
        self.assertIs(listed[0]["settledOnArrival"], True)

    def test_a_total_that_is_not_the_sum_of_its_parts_is_refused(self):
        """Without this, the clean run above could be a check that never fires."""
        clearing, _ = self.a_payment_of(("D1", "1190.00"), ("D2", "2380.00"))

        out = self.tell(clearing, total="3000.00")

        message = self.only_message(out["receipt"])
        self.assertTrue(message["accepted"],
                        "a readable document is still acknowledged")
        rules = [d["rule"] for d in message["disagreements"]]
        self.assertIn("remittance-total-not-parts", rules, message)

    def test_an_advice_dated_after_the_payment_settles_is_refused(self):
        """The two mocks keep separate clocks: this date is judged by mock-edi's."""
        ahead = (datetime.date.today()
                 + datetime.timedelta(days=30)).strftime("%Y%m%d")
        clearing, _ = self.a_payment_of(("E1", "1190.00"), date=ahead)

        out = self.tell(clearing)

        self.assertEqual(out["advice"]["settles"], ahead,
                         "SAP dated the advice by the payment it advises")
        rules = [d["rule"] for d in self.only_message(out["receipt"])["disagreements"]]
        self.assertIn("remitted-before-settlement", rules, out["receipt"])


class ReadingWhatSapWrote(unittest.TestCase):
    """The conversion on its own, with no mocks involved."""

    ADVICE = ('<?xml version="1.0" encoding="utf-8"?><PEXR2002><IDOC BEGIN="1">'
              '<E1IDKU1 SEGMENT="1"><BGMREF>0100000011</BGMREF>'
              '<E1EDK03 SEGMENT="1"><IDDAT>026</IDDAT><DATUM>20260927</DATUM>'
              "</E1EDK03>"
              '<E1EDKA1 SEGMENT="1"><PARVW>LF</PARVW><LIFNR>1000009</LIFNR>'
              "</E1EDKA1>"
              '<E1IDPU5 SEGMENT="1"><MOAQUAL>001</MOAQUAL>'
              "<MOABETR>3570.00-</MOABETR><CUXWAERZ>EUR</CUXWAERZ></E1IDPU5>"
              '<E1IDPU1 SEGMENT="1">'
              '<E1EDP02 SEGMENT="1"><QUALF>009</QUALF><BELNR>INV-1</BELNR>'
              "</E1EDP02>"
              '<E1IDPU5 SEGMENT="1"><MOAQUAL>001</MOAQUAL>'
              "<MOABETR>1190.00-</MOABETR><CUXWAERZ>EUR</CUXWAERZ></E1IDPU5>"
              "</E1IDPU1>"
              '<E1IDPU1 SEGMENT="1">'
              '<E1EDP02 SEGMENT="1"><QUALF>009</QUALF><BELNR>INV-2</BELNR>'
              "</E1EDP02>"
              '<E1IDPU5 SEGMENT="1"><MOAQUAL>001</MOAQUAL>'
              "<MOABETR>2380.00-</MOABETR><CUXWAERZ>EUR</CUXWAERZ></E1IDPU5>"
              "</E1IDPU1></E1IDKU1></IDOC></PEXR2002>")

    def test_the_minus_after_the_number_is_the_direction(self):
        advice = read_pexr2002(self.ADVICE)
        self.assertEqual(advice["total"], Decimal("3570.00"))
        self.assertEqual([row["amount"] for row in advice["invoices"]],
                         [Decimal("1190.00"), Decimal("2380.00")])

    def test_an_amount_that_is_not_money_out_is_refused_not_relabelled(self):
        """A credit on our account is not an advice; saying it is would be a lie."""
        with self.assertRaises(NotMoneyOut) as refused:
            read_pexr2002(self.ADVICE.replace("3570.00-", "3570.00"))
        self.assertIn("money out", str(refused.exception))

    def test_bpr01_says_no_money_moves_on_this_document(self):
        """`I` keeps it an advice; a payment-order code would be refused."""
        interchange = advice_820(read_pexr2002(self.ADVICE), US)
        self.assertIn("BPR*I*3570.00*C*NON", interchange)
        self.assertIn("*20260927~", interchange, "BPR16 is the settlement date")
        self.assertIn("TRN*1*0100000011~", interchange)
        self.assertIn("GS*RA*", interchange, "the functional group for an 820")


if __name__ == "__main__":
    unittest.main()
