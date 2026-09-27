"""Reading a bank statement: what a FINSTA01 claims, before anything acts on it.

The parser is a function, and testing it directly is what determinism asks
for; posting a statement over HTTP, and what it clears, is tested elsewhere.
"""
from __future__ import annotations

import os
import sys
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mocksap.odata import SapError  # noqa: E402
from mocksap.statement import balances_add_up, parse  # noqa: E402


def amount(qualifier, value, currency="EUR"):
    return ('<E1IDPU5 SEGMENT="1"><MOAQUAL>%s</MOAQUAL><MOABETR>%s</MOABETR>'
            "<CUXWAERZ>%s</CUXWAERZ></E1IDPU5>" % (qualifier, value, currency))


def line(number, value="1190.00", reference="SUP-9001", note="", qualifier="001"):
    ref = ('<E1EDP02 SEGMENT="1"><QUALF>009</QUALF><BELNR>%s</BELNR></E1EDP02>'
           % reference) if reference else ""
    text = ""
    if note:
        chunks = [note[i:i + 70] for i in range(0, len(note), 70)]
        text = ('<E1IDT01 SEGMENT="1"><TXTVW>ZZ</TXTVW>%s</E1IDT01>' % "".join(
            "<TXT%02d>%s</TXT%02d>" % (n, chunk, n)
            for n, chunk in enumerate(chunks, 1)))
    return ('<E1IDPF1 SEGMENT="1"><LINLINEIT>%s</LINLINEIT>%s%s%s</E1IDPF1>'
            % (number, ref, text, amount(qualifier, value)))


def balances(opening="1000.00", closing="2190.00", debits="0.00",
             credits="1190.00", interim=False):
    open_code, close_code = ("020", "022") if interim else ("019", "021")
    return ('<E1IDPF1 SEGMENT="1"><LINLINEIT>000000</LINLINEIT>%s%s%s%s</E1IDPF1>'
            % (amount(open_code, opening), amount(close_code, closing),
               amount("023", debits), amount("024", credits)))


def finsta(*items, mestyp="FINSTA", idoctyp="FINSTA01", number="00042",
           date="20260927", header=True):
    head = (
        '<E1IDKU1 SEGMENT="1"><BGMREF>%s</BGMREF>'
        '<E1EDK03 SEGMENT="1"><IDDAT>026</IDDAT><DATUM>%s</DATUM></E1EDK03>'
        '<E1IDB02 SEGMENT="1"><FIIBKENN>37040044</FIIBKENN>'
        "<FIIKONTO>0532013000</FIIKONTO><FIIBLAND>DE</FIIBLAND>"
        "<FIIKWAER>EUR</FIIKWAER></E1IDB02>%s</E1IDKU1>"
        % (number, date, "".join(items))) if header else ""
    return (
        '<?xml version="1.0" encoding="utf-8"?><%s><IDOC BEGIN="1">'
        '<EDI_DC40 SEGMENT="1"><IDOCTYP>%s</IDOCTYP><MESTYP>%s</MESTYP>'
        "</EDI_DC40>%s</IDOC></%s>" % (idoctyp, idoctyp, mestyp, head, idoctyp)
    ).encode("utf-8")


class TestACleanStatement(unittest.TestCase):
    def setUp(self):
        self.statement = parse(finsta(balances(), line("000001")))

    def test_the_header_says_which_account_and_which_statement(self):
        self.assertEqual(self.statement["account"], {
            "bank": "37040044", "number": "0532013000",
            "country": "DE", "currency": "EUR"})
        self.assertEqual(self.statement["statement"], "00042")
        self.assertEqual(self.statement["date"], "2026-09-27")

    def test_balances_come_from_sap_own_qualifiers_as_decimals(self):
        s = self.statement
        self.assertEqual((s["opening"], s["closing"], s["total_debits"],
                          s["total_credits"]),
                         (Decimal("1000.00"), Decimal("2190.00"),
                          Decimal("0.00"), Decimal("1190.00")))
        self.assertIsInstance(s["opening"], Decimal)
        self.assertFalse(s["interim"])

    def test_a_balance_carrier_is_not_a_movement(self):
        self.assertEqual([l["line"] for l in self.statement["lines"]], ["000001"])

    def test_the_line_carries_its_reference_and_amount(self):
        only = self.statement["lines"][0]
        self.assertEqual(only["reference"], "SUP-9001")
        self.assertEqual(only["qualifier"], "009")
        self.assertEqual(only["amount"], Decimal("1190.00"))
        self.assertEqual(only["currency"], "EUR")

    def test_the_side_is_unknown_rather_than_guessed(self):
        self.assertIsNone(self.statement["lines"][0]["side"])

    def test_it_adds_up(self):
        self.assertIsNone(balances_add_up(self.statement))

    def test_the_edifact_codes_are_not_balances(self):
        # 174/300 are UN/EDIFACT 5025; SAP's domain EDIF5025 does not use them.
        statement = parse(finsta(
            '<E1IDPF1 SEGMENT="1">%s%s</E1IDPF1>'
            % (amount("174", "1000.00"), amount("300", "2190.00"))))
        self.assertIsNone(statement["opening"])
        self.assertIsNone(statement["closing"])


class TestTheNoteToPayee(unittest.TestCase):
    def test_no_structured_reference_leaves_the_invoice_number_in_the_note(self):
        note = ("Payment for invoice SUP-9001 of 26 September, thank you. " * 3).strip()
        statement = parse(finsta(line("000001", reference="", note=note)))
        only = statement["lines"][0]

        self.assertEqual(only["reference"], "", "blank, not None, not guessed")
        self.assertIn("SUP-9001", only["note_to_payee"])
        self.assertEqual(only["note_lines"],
                         [note[i:i + 70] for i in range(0, len(note), 70)])

    def test_a_reference_wrapped_across_two_lines_survives_in_the_lines(self):
        # The bank wraps at 70 wherever it falls; here, inside SUP-9001.
        note = "x" * 66 + "SUP-9001 paid"
        only = parse(finsta(line("000001", reference="", note=note)))["lines"][0]

        self.assertEqual(only["note_to_payee"], "x" * 66 + "SUP- 9001 paid",
                         "joined with a space, as the contract says")
        self.assertIn("SUP-9001", "".join(only["note_lines"]))


class TestAnInterimStatement(unittest.TestCase):
    def test_interim_balances_are_read_and_flagged(self):
        statement = parse(finsta(balances(interim=True), line("000001")))
        self.assertTrue(statement["interim"])
        self.assertEqual(statement["opening"], Decimal("1000.00"))
        self.assertEqual(statement["closing"], Decimal("2190.00"))

    def test_a_final_statement_is_not_interim(self):
        self.assertFalse(parse(finsta(balances()))["interim"])


class TestAStatementThatDoesNotAddUp(unittest.TestCase):
    def test_it_says_both_numbers(self):
        statement = parse(finsta(balances(closing="2000.00"), line("000001")))
        problem = balances_add_up(statement)
        self.assertIsNotNone(problem)
        self.assertIn("2190.00", problem)
        self.assertIn("2000.00", problem)
        self.assertIn("00042", problem)

    def test_debits_count_against_the_opening_balance(self):
        statement = parse(finsta(balances(opening="1000.00", credits="0.00",
                                          debits="1190.00", closing="-190.00")))
        self.assertIsNone(balances_add_up(statement))

    def test_a_statement_without_balances_is_not_reported_as_wrong(self):
        self.assertIsNone(balances_add_up(parse(finsta(line("000001")))))


class TestSeveralAmountsOnOneLine(unittest.TestCase):
    def test_no_amount_is_picked_when_the_line_carries_two(self):
        statement = parse(finsta(
            '<E1IDPF1 SEGMENT="1"><LINLINEIT>000001</LINLINEIT>%s%s</E1IDPF1>'
            % (amount("001", "1190.00"), amount("009", "2.50"))))
        only = statement["lines"][0]
        self.assertIsNone(only["amount"])
        self.assertEqual([(a["qualifier"], a["amount"]) for a in only["amounts"]],
                         [("001", Decimal("1190.00")), ("009", Decimal("2.50"))])

    def test_a_trailing_minus_is_a_negative_amount(self):
        only = parse(finsta(line("000001", value="1190.00-")))["lines"][0]
        self.assertEqual(only["amount"], Decimal("-1190.00"))


class TestWhatIsNotAStatement(unittest.TestCase):
    def assertRefused(self, body, words):
        with self.assertRaises(SapError) as caught:
            parse(body)
        self.assertEqual(caught.exception.status, 400)
        self.assertIn(words, caught.exception.message)

    def test_another_idoc_type(self):
        self.assertRefused(finsta(idoctyp="INVOIC02", mestyp="INVOIC"), "INVOIC02")

    def test_lockbox(self):
        self.assertRefused(finsta(mestyp="LOCKBX"), "lockbox")

    def test_no_header(self):
        self.assertRefused(finsta(header=False), "E1IDKU1")

    def test_a_flat_file(self):
        self.assertRefused(b"EDI_DC40  800000000000001234567", "XML")

    def test_an_amount_that_is_not_a_number(self):
        self.assertRefused(finsta(line("000001", value="12,50")), "12,50")

    def test_an_empty_statement_is_still_a_statement(self):
        # mock-bank sends one every business day, empty days included.
        statement = parse(finsta(balances(closing="1000.00", credits="0.00")))
        self.assertEqual(statement["lines"], [])
        self.assertIsNone(balances_add_up(statement))


if __name__ == "__main__":
    unittest.main()
