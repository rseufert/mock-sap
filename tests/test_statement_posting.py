"""Posting a bank statement: clearing what it paid, reopening what came back.

The other half of #57. `statement.py` says what the file claims; this is what
the claim does to the open items, which is where it can be expensively wrong.
"""
from __future__ import annotations

import unittest

from support import MockServerCase, balances, finsta, invoic, line

CUBE = ("/sap/opu/odata/sap/API_OPLACCTGDOCITEMCUBE_SRV"
        "/A_OperationalAcctgDocItemCube")
INVOICE_SRV = "/sap/opu/odata/sap/API_SUPPLIERINVOICE_PROCESS_SRV"


class StatementCase(MockServerCase):
    def send(self, body):
        headers = dict(self.csrf_token(), **{"Content-Type": "application/xml",
                                             "Accept": "application/json"})
        status, _, receipt = self.request("POST", "/sap/bc/idoc", body=body,
                                          headers=headers)
        self.assertEqual(status, 201)
        return receipt

    def bill(self, reference, gross="1190.00", supplier="1000009",
             currency="EUR"):
        """A supplier invoice, so there is something open to pay."""
        net = "%.2f" % (float(gross) / 1.19)
        tax = "%.2f" % (float(gross) - float(net))
        return self.send(invoic(reference, gross, net, tax, supplier,
                                currency))["APPLIED"][0]

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

    def test_the_payment_s_own_line_is_cleared_on_the_day_it_posted(self):
        """Named as cleared and dated as open is two answers (#160)."""
        self.bill("SUP-B2", "1190.00")
        applied = self.send(finsta(
            line("000001", "1190.00-", reference="SUP-B2")))["APPLIED"][0]

        own = self.item_of(applied["CLEARED"][0]["CLEARINGDOCUMENT"])

        self.assertEqual(own["ClearingAccountingDocument"],
                         own["AccountingDocument"], "it clears itself")
        self.assertTrue(own["PostingDate"])
        self.assertEqual(own["ClearingDate"], own["PostingDate"])
        self.assertEqual(own["ClearingCreationDate"], own["PostingDate"])

    def test_it_is_cleared_on_the_same_day_as_the_invoice_it_paid(self):
        invoice = self.bill("SUP-B3", "1190.00")
        applied = self.send(finsta(
            line("000001", "1190.00-", reference="SUP-B3")))["APPLIED"][0]

        paid = self.item_of(invoice["ACCOUNTINGDOCUMENT"])
        own = self.item_of(applied["CLEARED"][0]["CLEARINGDOCUMENT"])

        self.assertEqual(own["ClearingDate"], paid["ClearingDate"])
        self.assertEqual(own["ClearingCreationDate"],
                         paid["ClearingCreationDate"])

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


class TestWhenTwoSuppliersShareAnInvoiceNumber(StatementCase):
    """#87: an invoice number is one supplier's sequence, not a key.

    Two suppliers both numbering an invoice `INV-100` is ordinary, and nothing
    the mock reads off a statement line says which was paid. Clearing whichever
    was found first paid one supplier's invoice with another's money and left
    an item open for the next payment run to pay again - and since #107 it
    also decided which supplier's payment document the invoice landed in, so
    the wrong party was credited as well.
    """

    def test_a_line_that_fits_two_suppliers_clears_neither(self):
        first = self.bill("SHARED-1", "1190.00", supplier="1000009")
        second = self.bill("SHARED-1", "1190.00", supplier="1000010")
        before = {r["AccountingDocument"] for r in self.open_payables()}

        applied = self.send(finsta(
            line("000001", "1190.00-", reference="SHARED-1")))["APPLIED"][0]

        self.assertEqual(applied["CLEARED"], [])
        self.assertEqual(before,
                         {r["AccountingDocument"] for r in self.open_payables()},
                         "both suppliers are still owed")
        reason = applied["UNPROCESSED"][0]["REASON"]
        for named in (first["ACCOUNTINGDOCUMENT"], second["ACCOUNTINGDOCUMENT"],
                      "1000009", "1000010"):
            self.assertIn(named, reason,
                          "the reason has to name what a person now has to "
                          "tell apart by hand")

    def test_the_amount_still_tells_them_apart(self):
        """The party decides only what reference and amount left undecided."""
        theirs = self.bill("SHARED-2", "1190.00", supplier="1000009")
        ours = self.bill("SHARED-2", "500.00", supplier="1000010")

        applied = self.send(finsta(
            line("000001", "500.00-", reference="SHARED-2")))["APPLIED"][0]

        self.assertEqual(applied["UNPROCESSED"], [])
        cleared = applied["CLEARED"][0]
        self.assertEqual(cleared["ACCOUNTINGDOCUMENT"],
                         ours["ACCOUNTINGDOCUMENT"])
        self.assertIn(theirs["ACCOUNTINGDOCUMENT"],
                      [r["AccountingDocument"] for r in self.open_payables()],
                      "and the other supplier is still owed 1190.00")
        self.assertEqual(self.item_of(cleared["CLEARINGDOCUMENT"])["Supplier"],
                         "1000010",
                         "the payment credits the supplier whose invoice it was")

    def test_a_wrong_amount_against_a_shared_number_names_every_candidate(self):
        """The mismatch stops naming one of several candidates arbitrarily."""
        first = self.bill("SHARED-3", "1190.00", supplier="1000009")
        second = self.bill("SHARED-3", "500.00", supplier="1000010")

        applied = self.send(finsta(
            line("000001", "700.00-", reference="SHARED-3")))["APPLIED"][0]

        self.assertEqual(applied["CLEARED"], [])
        reason = applied["UNPROCESSED"][0]["REASON"]
        for named in (first["ACCOUNTINGDOCUMENT"], second["ACCOUNTINGDOCUMENT"],
                      "1190.00", "500.00", "700.00"):
            self.assertIn(named, reason)

    def test_two_lines_for_two_suppliers_are_both_reported(self):
        """The limit this draws, said out loud rather than papered over.

        Two lines of 1190.00 against two items of 1190.00 do add up, but
        which line paid which supplier is not in the file - and that is what
        decides whose payment document each invoice lands in. Pairing them by
        the order the database returned them was the arbitrary part; refusing
        is the honest one.
        """
        self.bill("SHARED-4", "1190.00", supplier="1000009")
        self.bill("SHARED-4", "1190.00", supplier="1000010")
        before = {r["AccountingDocument"] for r in self.open_payables()}

        applied = self.send(finsta(
            line("000001", "1190.00-", reference="SHARED-4")
            + line("000002", "1190.00-", reference="SHARED-4")))["APPLIED"][0]

        self.assertEqual(applied["CLEARED"], [])
        self.assertEqual(len(applied["UNPROCESSED"]), 2)
        self.assertEqual(before,
                         {r["AccountingDocument"] for r in self.open_payables()})

    def test_one_supplier_billing_the_same_number_twice_is_still_paid(self):
        """A duplicate invoice is a different fault, and not this refusal.

        Either item clears the right supplier for the right money, so there
        is nothing here a person could tell apart by hand - which is the test
        of whether reporting a line is worth anything.
        """
        both = {self.bill("SHARED-5", "1190.00", supplier="1000009")
                ["ACCOUNTINGDOCUMENT"],
                self.bill("SHARED-5", "1190.00", supplier="1000009")
                ["ACCOUNTINGDOCUMENT"]}

        applied = self.send(finsta(
            line("000001", "1190.00-", reference="SHARED-5")))["APPLIED"][0]

        self.assertEqual(len(applied["CLEARED"]), 1, applied["UNPROCESSED"])
        self.assertIn(applied["CLEARED"][0]["ACCOUNTINGDOCUMENT"], both)
        open_now = {r["AccountingDocument"] for r in self.open_payables()}
        self.assertEqual(len(both & open_now), 1,
                         "the money was paid once, so one of the two is still "
                         "open and the duplicate is visible")

    def test_a_return_quoting_a_shared_number_reopens_neither(self):
        """The credit side runs the same match, so it refuses the same way.

        Reopening the wrong supplier's invoice is the mirror of clearing it:
        a payment run would pay money back out on a payment nobody returned.
        """
        first = self.bill("SHARED-6", "1190.00", supplier="1000009")
        self.send(finsta(line("000001", "1190.00-", reference="SHARED-6"),
                         statement="00070"))
        second = self.bill("SHARED-6", "1190.00", supplier="1000010")
        self.send(finsta(line("000001", "1190.00-", reference="SHARED-6"),
                         statement="00071"))

        applied = self.send(finsta(
            line("000001", "1190.00", reference="SHARED-6", action="RET"),
            statement="00072"))["APPLIED"][0]

        self.assertEqual(applied["REOPENED"], [])
        self.assertEqual(len(applied["UNPROCESSED"]), 1)
        open_now = {r["AccountingDocument"] for r in self.open_payables()}
        self.assertNotIn(first["ACCOUNTINGDOCUMENT"], open_now)
        self.assertNotIn(second["ACCOUNTINGDOCUMENT"], open_now,
                         "neither supplier's invoice came back open")


class TestWhenTheCurrencyDisagrees(StatementCase):
    """#88: an amount without a currency is not an amount.

    Matching compared the reference and the number, so a line for 1190.00 USD
    cleared a payable of 1190.00 EUR. That is the one disagreement which reads
    as agreement, and it is how a bank writing the wrong currency code and a
    mock not looking at it cancelled each other out for as long as they did.
    """

    def test_a_usd_line_does_not_clear_a_eur_payable(self):
        invoice = self.bill("CCY-1", "1190.00", currency="EUR")

        applied = self.send(finsta(
            line("000001", "1190.00-", reference="CCY-1", currency="USD"),
            statement="00080"))["APPLIED"][0]

        self.assertEqual(applied["CLEARED"], [])
        self.assertIn(invoice["ACCOUNTINGDOCUMENT"],
                      [r["AccountingDocument"] for r in self.open_payables()],
                      "still owed, in euros")
        reason = applied["UNPROCESSED"][0]["REASON"]
        for named in ("USD", "EUR", "1190.00",
                      invoice["ACCOUNTINGDOCUMENT"]):
            self.assertIn(named, reason)
        self.assertIn("not a payment", reason,
                      "the reason has to say the two numbers being equal is "
                      "not the point")

    def test_a_eur_line_does_not_clear_a_usd_payable(self):
        """The mirror, so the check is not one currency against the house's."""
        invoice = self.bill("CCY-2", "1190.00", currency="USD")

        applied = self.send(finsta(
            line("000001", "1190.00-", reference="CCY-2"),
            statement="00081"))["APPLIED"][0]

        self.assertEqual(applied["CLEARED"], [])
        self.assertIn(invoice["ACCOUNTINGDOCUMENT"],
                      [r["AccountingDocument"] for r in self.open_payables()])

    def test_a_line_in_the_payables_own_currency_clears_it(self):
        invoice = self.bill("CCY-3", "1190.00", currency="USD")

        applied = self.send(finsta(
            line("000001", "1190.00-", reference="CCY-3", currency="USD"),
            statement="00082"))["APPLIED"][0]

        self.assertEqual(applied["UNPROCESSED"], [])
        cleared = applied["CLEARED"][0]
        self.assertEqual(cleared["ACCOUNTINGDOCUMENT"],
                         invoice["ACCOUNTINGDOCUMENT"])
        self.assertEqual(self.item_of(cleared["CLEARINGDOCUMENT"])
                         ["TransactionCurrency"], "USD",
                         "and the payment is posted in that currency")

    def test_a_currency_code_is_read_whatever_case_it_arrives_in(self):
        """`usd` is USD. Normalising a code is not guessing at one."""
        invoice = self.bill("CCY-4", "1190.00", currency="USD")

        applied = self.send(finsta(
            line("000001", "1190.00-", reference="CCY-4", currency=" usd "),
            statement="00083"))["APPLIED"][0]

        self.assertEqual(applied["UNPROCESSED"], [])
        self.assertEqual(applied["CLEARED"][0]["ACCOUNTINGDOCUMENT"],
                         invoice["ACCOUNTINGDOCUMENT"])

    def test_the_currency_tells_two_suppliers_apart(self):
        """What #87 refuses is only what nothing in the file separates.

        The same invoice number for the same number of units of different
        money is two payables, and the line says which one it paid.
        """
        theirs = self.bill("CCY-5", "1190.00", supplier="1000009", currency="EUR")
        ours = self.bill("CCY-5", "1190.00", supplier="1000010", currency="USD")

        applied = self.send(finsta(
            line("000001", "1190.00-", reference="CCY-5", currency="USD"),
            statement="00084"))["APPLIED"][0]

        self.assertEqual(applied["UNPROCESSED"], [])
        self.assertEqual(applied["CLEARED"][0]["ACCOUNTINGDOCUMENT"],
                         ours["ACCOUNTINGDOCUMENT"])
        self.assertIn(theirs["ACCOUNTINGDOCUMENT"],
                      [r["AccountingDocument"] for r in self.open_payables()])

    def test_a_statement_that_names_no_currency_clears_nothing(self):
        """No `CUXWAERZ` on the line and no `FIIKWAER` on the account.

        There is nothing to compare and nothing a mock may assume: filling in
        the house currency would be inventing the half of the amount that
        decides whether this is a payment at all.
        """
        invoice = self.bill("CCY-6", "1190.00", currency="EUR")

        applied = self.send(finsta(
            line("000001", "1190.00-", reference="CCY-6", currency=""),
            statement="00085", currency=""))["APPLIED"][0]

        self.assertEqual(applied["CLEARED"], [])
        self.assertIn("currency", applied["UNPROCESSED"][0]["REASON"])
        self.assertIn(invoice["ACCOUNTINGDOCUMENT"],
                      [r["AccountingDocument"] for r in self.open_payables()])

    def test_a_return_in_another_currency_reopens_nothing(self):
        """The credit side runs the same match, so it compares the same way.

        A euro credit quoting a dollar payment would reverse a clearing that
        was never made in that money, and put the invoice back among the open
        items for the next payment run to pay again.
        """
        invoice = self.bill("CCY-7", "1190.00", currency="USD")
        self.send(finsta(line("000001", "1190.00-", reference="CCY-7",
                              currency="USD"), statement="00086"))

        applied = self.send(finsta(
            line("000001", "1190.00", reference="CCY-7", action="RET"),
            statement="00087"))["APPLIED"][0]

        self.assertEqual(applied["REOPENED"], [])
        self.assertEqual(len(applied["UNPROCESSED"]), 1)
        self.assertNotIn(invoice["ACCOUNTINGDOCUMENT"],
                         [r["AccountingDocument"] for r in self.open_payables()],
                         "the paid invoice stayed paid")


class TestWhenOneInvoiceNumberContainsAnother(StatementCase):
    """#86: a structured reference is believed over a substring of the note.

    `INV-1` appears inside `INV-10`, so a note naming the invoice the bank
    actually paid also matched the shorter number of a different invoice. The
    structured reference was compared first but was not decisive: failing it
    fell through to searching the note, so a line that said exactly which
    invoice it paid was matched to one it did not.

    What that cost depended on who the other invoice belonged to. The same
    supplier: the wrong invoice cleared and the right one stayed open for the
    next payment run to pay a second time. Two suppliers: the line became
    ambiguous under #87 and cleared nothing, which is a refusal the bank had
    already answered.
    """

    def test_the_wrong_invoice_is_not_the_one_that_clears(self):
        shorter = self.bill("LONG-1", "1190.00")
        named = self.bill("LONG-10", "1190.00")

        applied = self.send(finsta(line(
            "000001", "1190.00-", reference="LONG-10",
            note="PAYMENT FOR LONG-10")))["APPLIED"][0]

        self.assertEqual(len(applied["CLEARED"]), 1, applied["UNPROCESSED"])
        self.assertEqual(applied["CLEARED"][0]["ACCOUNTINGDOCUMENT"],
                         named["ACCOUNTINGDOCUMENT"],
                         "the invoice the bank named is the one that clears")
        self.assertIn(shorter["ACCOUNTINGDOCUMENT"],
                      [r["AccountingDocument"] for r in self.open_payables()],
                      "and LONG-1 is still owed, not paid by LONG-10's money")

    def test_and_the_shorter_number_is_still_paid_when_it_is_the_one_named(self):
        """The rule is 'believe the reference', not 'prefer the longer'."""
        shorter = self.bill("LONG-2", "1190.00")
        self.bill("LONG-20", "1190.00")

        applied = self.send(finsta(line(
            "000001", "1190.00-", reference="LONG-2",
            note="PAYMENT FOR LONG-2")))["APPLIED"][0]

        self.assertEqual(len(applied["CLEARED"]), 1, applied["UNPROCESSED"])
        self.assertEqual(applied["CLEARED"][0]["ACCOUNTINGDOCUMENT"],
                         shorter["ACCOUNTINGDOCUMENT"])

    def test_a_line_two_suppliers_seemed_to_fit_is_decided_by_its_reference(self):
        """#87's refusal was reached by #86's false match.

        Each supplier has one of the two numbers, so the substring match made
        the line look like it fitted both parties and nothing was cleared -
        for a line whose structured reference says which invoice was paid.
        """
        theirs = self.bill("LONG-3", "1190.00", supplier="1000009")
        ours = self.bill("LONG-30", "1190.00", supplier="1000010")

        applied = self.send(finsta(line(
            "000001", "1190.00-", reference="LONG-30",
            note="PAYMENT FOR LONG-30")))["APPLIED"][0]

        self.assertEqual(applied["UNPROCESSED"], [],
                         "the bank said which invoice, so nothing is ambiguous")
        self.assertEqual(applied["CLEARED"][0]["ACCOUNTINGDOCUMENT"],
                         ours["ACCOUNTINGDOCUMENT"])
        self.assertIn(theirs["ACCOUNTINGDOCUMENT"],
                      [r["AccountingDocument"] for r in self.open_payables()])

    def test_a_structured_reference_that_names_nothing_open_ends_it(self):
        """The cost of the rule, stated: the note is not a second chance.

        A line carrying a structured reference is answered on that reference
        alone. Where the bank quotes something this mock has no open item for,
        the line is reported rather than matched on prose that happens to name
        an invoice - which is the same refusal a treasury team works through,
        and the alternative is guessing against what the bank said.
        """
        invoice = self.bill("LONG-4", "1190.00")

        applied = self.send(finsta(line(
            "000001", "1190.00-", reference="PAYRUN-2026-04",
            note="PAYMENT FOR LONG-4")))["APPLIED"][0]

        self.assertEqual(applied["CLEARED"], [])
        self.assertIn(invoice["ACCOUNTINGDOCUMENT"],
                      [r["AccountingDocument"] for r in self.open_payables()])
        self.assertIn("no open item quotes this reference",
                      applied["UNPROCESSED"][0]["REASON"])


class TestAReturnedPayment(StatementCase):
    def test_a_credit_reopens_the_item_it_paid(self):
        invoice = self.bill("SUP-G1", "1190.00")
        self.send(finsta(line("000001", "1190.00-", reference="SUP-G1"),
                         statement="00050"))
        self.assertNotIn(invoice["ACCOUNTINGDOCUMENT"],
                         [r["AccountingDocument"] for r in self.open_payables()])

        applied = self.send(finsta(
            line("000001", "1190.00", reference="SUP-G1", action="RET"),
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
        self.send(finsta(line("000001", "1190.00", reference="SUP-G2", action="RET"),
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


    def test_an_item_open_again_does_not_say_when_it_was_cleared(self):
        returned = self.bill("SUP-G4", "1190.00")
        self.send(finsta(line("000001", "1190.00-", reference="SUP-G4"),
                         statement="00062"))
        cleared = self.item_of(returned["ACCOUNTINGDOCUMENT"])
        self.assertTrue(cleared["ClearingCreationDate"], "something to lose")

        self.send(finsta(line("000001", "1190.00", reference="SUP-G4", action="RET"),
                         statement="00063"))

        reopened = self.item_of(returned["ACCOUNTINGDOCUMENT"])
        self.assertIsNone(reopened["ClearingDate"])
        self.assertIsNone(reopened["ClearingCreationDate"],
                          "the day a clearing was entered goes with the clearing")
        self.assertTrue(reopened["ClearingIsReversed"],
                        "which is what still says it was paid once")

    def test_the_return_s_own_line_is_cleared_on_the_day_it_posted(self):
        self.bill("SUP-G5", "1190.00")
        self.send(finsta(line("000001", "1190.00-", reference="SUP-G5"),
                         statement="00064"))
        applied = self.send(finsta(
            line("000001", "1190.00", reference="SUP-G5", action="RET"),
            statement="00065"))["APPLIED"][0]

        own = self.item_of(applied["REOPENED"][0]["REVERSALDOCUMENT"])

        self.assertEqual(own["ClearingAccountingDocument"],
                         own["AccountingDocument"])
        self.assertTrue(own["PostingDate"])
        self.assertEqual(own["ClearingDate"], own["PostingDate"])
        self.assertEqual(own["ClearingCreationDate"], own["PostingDate"])


class TestWhenMoneyArrivesQuotingAPaidInvoice(StatementCase):
    """A credit has to say which kind it is, and is read as neither until it
    does (#89).

    Money in has two readings that are opposites. A payment of ours coming
    back reopens the invoice; money arriving - a refund, a credit note, a
    supplier returning an overpayment - does not. The sign cannot tell them
    apart, so a credit quoting an invoice already paid used to reopen it on
    the strength of nothing, and the next payment run paid it a second time.
    """

    def test_money_arriving_that_quotes_a_paid_invoice_reopens_nothing(self):
        """The fault itself: a refund quoting the invoice it refunds."""
        invoice = self.bill("ARRIVE-1", "1190.00")
        self.send(finsta(line("000001", "1190.00-", reference="ARRIVE-1"),
                         statement="00088"))

        applied = self.send(finsta(
            line("000001", "1190.00", reference="ARRIVE-1", action="RCV"),
            statement="00089"))["APPLIED"][0]

        self.assertEqual(applied["REOPENED"], [],
                         "money arriving is not a payment coming back")
        self.assertNotIn(invoice["ACCOUNTINGDOCUMENT"],
                         [r["AccountingDocument"] for r in self.open_payables()],
                         "still settled, so no payment run pays it again")

    def test_an_invoice_refunded_is_not_an_invoice_owed(self):
        """The data, not just the response: the clearing is untouched.

        `REOPENED` being empty is the report. What decides whether the next
        payment run pays this again is the item itself.
        """
        invoice = self.bill("ARRIVE-2", "1190.00")
        self.send(finsta(line("000001", "1190.00-", reference="ARRIVE-2"),
                         statement="00090"))
        self.send(finsta(
            line("000001", "1190.00", reference="ARRIVE-2", action="RCV"),
            statement="00091"))

        item = self.item_of(invoice["ACCOUNTINGDOCUMENT"])

        self.assertNotEqual(item["ClearingAccountingDocument"], "",
                            "the payment that settled it still did")
        self.assertFalse(item["ClearingIsReversed"],
                         "nothing was reversed, so nothing says it was")

    def test_a_credit_that_does_not_say_which_kind_reopens_nothing(self):
        """Undeclared is read as neither, and reported rather than guessed."""
        invoice = self.bill("ARRIVE-3", "1190.00")
        self.send(finsta(line("000001", "1190.00-", reference="ARRIVE-3"),
                         statement="00092"))

        applied = self.send(finsta(line("000001", "1190.00",
                                        reference="ARRIVE-3"),
                                   statement="00093"))["APPLIED"][0]

        self.assertEqual(applied["REOPENED"], [])
        self.assertEqual(len(applied["UNPROCESSED"]), 1)
        self.assertIn("does not say which kind",
                      applied["UNPROCESSED"][0]["REASON"])
        self.assertNotIn(invoice["ACCOUNTINGDOCUMENT"],
                         [r["AccountingDocument"] for r in self.open_payables()])

    def test_one_statement_reopens_only_the_payment_that_says_it_came_back(self):
        """The declaration is what does the work, not the amount or the sign.

        Two credits for the same money against two paid invoices, in one file,
        differing in nothing but what they say they are.
        """
        returned = self.bill("ARRIVE-4", "1190.00")
        refunded = self.bill("ARRIVE-5", "1190.00")
        self.send(finsta(line("000001", "1190.00-", reference="ARRIVE-4")
                         + line("000002", "1190.00-", reference="ARRIVE-5"),
                         statement="00094"))

        applied = self.send(finsta(
            line("000001", "1190.00", reference="ARRIVE-4", action="RET")
            + line("000002", "1190.00", reference="ARRIVE-5", action="RCV"),
            statement="00095"))["APPLIED"][0]

        self.assertEqual(len(applied["REOPENED"]), 1, applied["UNPROCESSED"])
        self.assertEqual(applied["REOPENED"][0]["ACCOUNTINGDOCUMENT"],
                         returned["ACCOUNTINGDOCUMENT"])
        open_now = {r["AccountingDocument"] for r in self.open_payables()}
        self.assertIn(returned["ACCOUNTINGDOCUMENT"], open_now)
        self.assertNotIn(refunded["ACCOUNTINGDOCUMENT"], open_now)

    def test_an_action_is_read_whatever_case_it_arrives_in(self):
        """`ret` is `RET`: the code is a code, not a byte sequence."""
        invoice = self.bill("ARRIVE-6", "1190.00")
        self.send(finsta(line("000001", "1190.00-", reference="ARRIVE-6"),
                         statement="00096"))

        applied = self.send(finsta(
            line("000001", "1190.00", reference="ARRIVE-6", action=" ret "),
            statement="00097"))["APPLIED"][0]

        self.assertEqual(len(applied["REOPENED"]), 1, applied["UNPROCESSED"])
        self.assertIn(invoice["ACCOUNTINGDOCUMENT"],
                      [r["AccountingDocument"] for r in self.open_payables()])

    def test_an_action_nobody_defined_is_not_read_as_a_return(self):
        """Only the two values mean anything; a third is still undeclared.

        Reading "it said *something*" as a return would put every writer's
        private code back on the path this closed.
        """
        invoice = self.bill("ARRIVE-7", "1190.00")
        self.send(finsta(line("000001", "1190.00-", reference="ARRIVE-7"),
                         statement="00098"))

        applied = self.send(finsta(
            line("000001", "1190.00", reference="ARRIVE-7", action="900"),
            statement="00100"))["APPLIED"][0]

        self.assertEqual(applied["REOPENED"], [])
        self.assertIn("does not say which kind",
                      applied["UNPROCESSED"][0]["REASON"])
        self.assertNotIn(invoice["ACCOUNTINGDOCUMENT"],
                         [r["AccountingDocument"] for r in self.open_payables()])

    def test_money_arriving_against_no_invoice_of_ours_is_still_money_in(self):
        """A receipt is reported as what it is, matched or not.

        It does not reach the matching at all - there is nothing for it to
        clear and nothing for it to reverse - so the reason is about the line
        rather than about the open items.
        """
        applied = self.send(finsta(
            line("000001", "500.00", reference="NOBODY-OWES-THIS",
                 action="RCV"),
            statement="00101"))["APPLIED"][0]

        self.assertEqual((applied["CLEARED"], applied["REOPENED"]), ([], []))
        self.assertIn("money arriving",
                      applied["UNPROCESSED"][0]["REASON"])

    def test_a_debits_action_is_not_consulted(self):
        """Money out has one reading, so nothing on it has to say so.

        A writer that puts `LINACTION` on every line, debits included, still
        gets its payments cleared.
        """
        invoice = self.bill("ARRIVE-8", "1190.00")

        applied = self.send(finsta(
            line("000001", "1190.00-", reference="ARRIVE-8", action="RCV"),
            statement="00102"))["APPLIED"][0]

        self.assertEqual(len(applied["CLEARED"]), 1, applied["UNPROCESSED"])
        self.assertNotIn(invoice["ACCOUNTINGDOCUMENT"],
                         [r["AccountingDocument"] for r in self.open_payables()])


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


class TestTheOutcomeOutlivesTheResponse(StatementCase):
    """#105: posting a statement decided something; reading it back says what.

    The POST receipt used to be the only place the decision existed, so a
    client that did not keep the response had no way back to it.
    """

    def idoc(self, docnum):
        status, _, record = self.get("/sap/bc/idoc/" + docnum)
        self.assertEqual(status, 200)
        return record

    def test_the_read_returns_exactly_what_the_receipt_said(self):
        paid = self.bill("OUT-A1", "1190.00")
        receipt = self.send(finsta(
            line("000001", "1190.00-", reference="OUT-A1")
            + line("000002", "500.00-", reference="NOT-A-THING")))

        read = self.idoc(receipt["DOCNUM"])
        self.assertEqual(read["APPLIED"], receipt["APPLIED"],
                         "the read is the receipt, not an approximation of it")
        self.assertEqual(read["APPLIED"][0]["CLEARED"][0]["ACCOUNTINGDOCUMENT"],
                         paid["ACCOUNTINGDOCUMENT"])

    def test_a_cleared_line_still_names_the_document_that_paid_it(self):
        self.bill("OUT-B1", "1190.00")
        receipt = self.send(finsta(line("000001", "1190.00-", reference="OUT-B1")))
        posted = receipt["APPLIED"][0]["CLEARED"][0]

        cleared = self.idoc(receipt["DOCNUM"])["APPLIED"][0]["CLEARED"][0]
        self.assertEqual(cleared["CLEARINGDOCUMENT"], posted["CLEARINGDOCUMENT"])
        self.assertEqual(cleared["REFERENCE"], "OUT-B1")
        self.assertEqual(cleared["AMOUNT"], posted["AMOUNT"])

    def test_a_refused_line_keeps_the_mocks_own_words(self):
        receipt = self.send(finsta(line("000007", "99.00-", reference="NOBODY")))
        said = receipt["APPLIED"][0]["UNPROCESSED"][0]["REASON"]

        refused = self.idoc(receipt["DOCNUM"])["APPLIED"][0]["UNPROCESSED"][0]
        self.assertEqual(refused["REASON"], said)
        self.assertEqual(refused["LINE"], "000007")
        self.assertTrue(said, "the reason is prose, and it is the point")

    def test_a_returned_payment_names_the_reversal_document(self):
        self.bill("OUT-C1", "1190.00")
        self.send(finsta(line("000001", "1190.00-", reference="OUT-C1"),
                         statement="00050"))
        receipt = self.send(finsta(
            line("000001", "1190.00", reference="OUT-C1", action="RET"),
            statement="00051"))
        posted = receipt["APPLIED"][0]["REOPENED"]
        self.assertEqual(len(posted), 1, "the credit reopened the invoice")

        reopened = self.idoc(receipt["DOCNUM"])["APPLIED"][0]["REOPENED"][0]
        self.assertEqual(reopened["REVERSALDOCUMENT"],
                         posted[0]["REVERSALDOCUMENT"])
        self.assertNotIn("CLEARINGDOCUMENT", reopened,
                         "a reopened line is not a cleared one")

    def test_a_statement_that_settled_nothing_still_says_it_posted(self):
        receipt = self.send(finsta(statement="00099"))
        read = self.idoc(receipt["DOCNUM"])

        self.assertIn("APPLIED", read,
                      "cleared nothing is a different answer from not a statement")
        applied = read["APPLIED"][0]
        self.assertEqual(applied["STATEMENT"], "00099")
        self.assertEqual((applied["CLEARED"], applied["REOPENED"],
                          applied["UNPROCESSED"]), ([], [], []))

    def test_an_invoic_outcome_is_not_read_back_as_a_statement(self):
        """An INVOIC has an outcome of its own since #116, and it is not this one.

        Before #116 this asserted an INVOIC had no outcome at all, which was
        only true while the statement was the one kind that persisted. What it
        was guarding is still worth guarding: the statement reader must not
        answer for an IDoc that posted something else.
        """
        receipt = self.send(invoic("OUT-D1"))
        applied = self.idoc(receipt["DOCNUM"])["APPLIED"][0]

        self.assertNotIn("STATEMENT", applied)
        self.assertNotIn("CLEARED", applied)
        self.assertIn("SUPPLIERINVOICE", applied,
                      "an INVOIC posted an invoice, not a statement")

    def test_what_a_statement_said_about_itself_survives_too(self):
        receipt = self.send(finsta(
            line("000001", "100.00-"), opening="1000.00", closing="5000.00",
            debits="100.00", credits_="0.00"))
        said = receipt["APPLIED"][0]["FINDINGS"]
        self.assertTrue(said, "this statement does not add up")
        self.assertEqual(self.idoc(receipt["DOCNUM"])["APPLIED"][0]["FINDINGS"],
                         said)

    def test_the_listing_stays_narrow(self):
        self.bill("OUT-E1", "1190.00")
        receipt = self.send(finsta(line("000001", "1190.00-", reference="OUT-E1")))

        _, _, body = self.get("/_mock/idocs")
        rows = [r for r in body["results"] if r["docnum"] == receipt["DOCNUM"]]
        self.assertEqual(len(rows), 1)
        self.assertNotIn("APPLIED", rows[0],
                         "one IDoc has one outcome; a listing of 50 would carry 50")


class TestOnePaymentPerPayee(StatementCase):
    """A payment run pays a supplier, not an invoice (#107).

    Until this, `apply_statement` posted a journal entry per statement line,
    so one payment document settled exactly one invoice - and a remittance
    advice generated from one could only ever name a single invoice, whose
    total trivially equals its single part.
    """

    JOURNAL = "/sap/opu/odata/sap/API_JOURNALENTRY_SRV"

    def entry(self, document):
        """A payment document with its lines, found without guessing its year."""
        _, _, body = self.get(
            self.JOURNAL + "/A_JournalEntry?$filter=AccountingDocument%%20eq%%20"
            "'%s'&$expand=to_JournalEntryItem&$format=json" % document)
        results = body["d"]["results"]
        self.assertEqual(len(results), 1, "one document for that number")
        return results[0]

    def test_two_invoices_from_one_supplier_share_one_payment(self):
        first = self.bill("PAY-A1", "1190.00")
        second = self.bill("PAY-A2", "2380.00")

        applied = self.send(finsta(
            line("000001", "1190.00-", reference="PAY-A1")
            + line("000002", "2380.00-", reference="PAY-A2"),
            statement="00070"))["APPLIED"][0]

        self.assertEqual(len(applied["CLEARED"]), 2)
        documents = {row["CLEARINGDOCUMENT"] for row in applied["CLEARED"]}
        self.assertEqual(len(documents), 1,
                         "one supplier, one statement, one payment")
        paying = documents.pop()
        for invoice in (first, second):
            self.assertEqual(self.item_of(invoice["ACCOUNTINGDOCUMENT"])
                             ["ClearingAccountingDocument"], paying)

    def test_the_payment_has_a_line_per_invoice_and_one_for_the_bank(self):
        self.bill("PAY-B1", "1190.00")
        self.bill("PAY-B2", "2380.00")
        applied = self.send(finsta(
            line("000001", "1190.00-", reference="PAY-B1")
            + line("000002", "2380.00-", reference="PAY-B2"),
            statement="00071"))["APPLIED"][0]
        paying = applied["CLEARED"][0]["CLEARINGDOCUMENT"]

        entry = self.entry(paying)
        lines = entry["to_JournalEntryItem"]["results"]
        payables = [x for x in lines if x["Supplier"]]
        bank = [x for x in lines if not x["Supplier"]]
        self.assertEqual(len(payables), 2, "one supplier line per invoice")
        self.assertEqual(len(bank), 1, "and a single credit to the bank")
        self.assertAlmostEqual(
            float(bank[0]["AmountInTransactionCurrency"]), 3570.00, places=2,
            msg="the bank pays the sum, which is what the advice must total")
        self.assertEqual(entry["AccountingDocumentType"], "ZP")
        self.assertEqual(entry["ReferenceDocument"], "00071",
                         "a payment's reference is the statement that made it")

    def test_each_invoice_points_at_its_own_line_of_the_payment(self):
        """`ClearingItem` was hardcoded `000001`, which only one line can be."""
        first = self.bill("PAY-C1", "1190.00")
        second = self.bill("PAY-C2", "2380.00")
        self.send(finsta(line("000001", "1190.00-", reference="PAY-C1")
                         + line("000002", "2380.00-", reference="PAY-C2"),
                         statement="00072"))

        items = [self.item_of(invoice["ACCOUNTINGDOCUMENT"])
                 for invoice in (first, second)]
        self.assertEqual([x["ClearingItem"] for x in items],
                         ["000001", "000002"])
        entry = self.entry(items[0]["ClearingAccountingDocument"])
        lines = {x["AccountingDocumentItem"]: x
                 for x in entry["to_JournalEntryItem"]["results"]}
        for invoice, item in zip((first, second), items):
            named = lines[item["ClearingItem"]]
            self.assertIn(invoice["ACCOUNTINGDOCUMENT"],
                          named["DocumentItemText"],
                          "the line it points at is the line that cleared it")

    def test_two_suppliers_in_one_statement_get_a_payment_each(self):
        self.bill("PAY-D1", "1190.00", supplier="1000009")
        self.bill("PAY-D2", "2380.00", supplier="1000010")

        applied = self.send(finsta(
            line("000001", "1190.00-", reference="PAY-D1")
            + line("000002", "2380.00-", reference="PAY-D2"),
            statement="00073"))["APPLIED"][0]

        self.assertEqual(len(applied["CLEARED"]), 2)
        self.assertEqual(
            len({row["CLEARINGDOCUMENT"] for row in applied["CLEARED"]}), 2,
            "a payment document belongs to one supplier")

    def test_a_grouped_payment_leaves_no_payables_of_its_own(self):
        """`_self_clear` assumed one subledger line; now there are several."""
        self.bill("PAY-E1", "1190.00")
        self.bill("PAY-E2", "2380.00")
        before = len(self.open_payables())

        applied = self.send(finsta(
            line("000001", "1190.00-", reference="PAY-E1")
            + line("000002", "2380.00-", reference="PAY-E2"),
            statement="00074"))["APPLIED"][0]

        self.assertEqual(len(self.open_payables()), before - 2,
                         "two fewer open items, not two fewer and two more")
        paying = applied["CLEARED"][0]["CLEARINGDOCUMENT"]
        self.assertNotIn(paying, [r["AccountingDocument"]
                                 for r in self.open_payables()])

    def test_the_outcome_still_reads_in_the_statement_s_line_order(self):
        """Grouping posts by payee; what a client is shown is the statement."""
        self.bill("PAY-F1", "1190.00", supplier="1000009")
        self.bill("PAY-F2", "2380.00", supplier="1000010")
        self.bill("PAY-F3", "500.00", supplier="1000009")

        applied = self.send(finsta(
            line("000001", "1190.00-", reference="PAY-F1")
            + line("000002", "2380.00-", reference="PAY-F2")
            + line("000003", "500.00-", reference="PAY-F3"),
            statement="00075"))["APPLIED"][0]

        self.assertEqual([row["LINE"] for row in applied["CLEARED"]],
                         ["000001", "000002", "000003"])
        first, second, third = applied["CLEARED"]
        self.assertEqual(first["CLEARINGDOCUMENT"], third["CLEARINGDOCUMENT"],
                         "lines 1 and 3 are the same supplier")
        self.assertNotEqual(first["CLEARINGDOCUMENT"],
                            second["CLEARINGDOCUMENT"])

    def test_two_lines_quoting_one_invoice_do_not_settle_it_twice(self):
        """Clearing used to take the item out of `_open_items` as it went."""
        invoice = self.bill("PAY-G1", "1190.00")

        applied = self.send(finsta(
            line("000001", "1190.00-", reference="PAY-G1")
            + line("000002", "1190.00-", reference="PAY-G1"),
            statement="00076"))["APPLIED"][0]

        self.assertEqual(len(applied["CLEARED"]), 1)
        self.assertEqual(len(applied["UNPROCESSED"]), 1)
        self.assertEqual(applied["UNPROCESSED"][0]["LINE"], "000002")
        item = self.item_of(invoice["ACCOUNTINGDOCUMENT"])
        self.assertEqual(item["ClearingAccountingDocument"],
                         applied["CLEARED"][0]["CLEARINGDOCUMENT"])

    def test_a_payable_in_another_currency_is_not_paid_by_the_same_document(self):
        """A document header carries one currency, whatever it shares a payee with.

        One supplier, two payables in different money, each paid by a line in
        its own: the two cannot share a payment document however much they
        share a payee.
        """
        self.bill("PAY-H1", "1190.00", supplier="1000009", currency="EUR")
        self.bill("PAY-H2", "2380.00", supplier="1000009", currency="USD")

        applied = self.send(finsta(
            line("000001", "1190.00-", reference="PAY-H1")
            + line("000002", "2380.00-", reference="PAY-H2", currency="USD"),
            statement="00077"))["APPLIED"][0]

        self.assertEqual(len(applied["CLEARED"]), 2, applied["UNPROCESSED"])
        paid_by = {row["REFERENCE"]: row["CLEARINGDOCUMENT"]
                   for row in applied["CLEARED"]}
        self.assertEqual(len(set(paid_by.values())), 2)
        self.assertEqual(
            self.item_of(paid_by["PAY-H1"])["TransactionCurrency"], "EUR")
        self.assertEqual(
            self.item_of(paid_by["PAY-H2"])["TransactionCurrency"], "USD")


class TestWhenAStatementPaysAndTakesItBack(StatementCase):
    """Returns post after the payments, whatever order the lines are in (#107)."""

    def test_a_credit_above_the_debit_it_returns_still_reopens_it(self):
        """While clearing posted line by line, this credit matched nothing.

        It is a behaviour change, and the honest one: a statement is a day's
        movements on an account, and nothing says the bank lists a payment
        before the return of it.
        """
        invoice = self.bill("BACK-A1", "1190.00")

        applied = self.send(finsta(
            line("000001", "1190.00", reference="BACK-A1", action="RET")      # money in
            + line("000002", "1190.00-", reference="BACK-A1"),  # money out
            statement="00078"))["APPLIED"][0]

        self.assertEqual(len(applied["CLEARED"]), 1, applied["UNPROCESSED"])
        self.assertEqual(len(applied["REOPENED"]), 1, applied["UNPROCESSED"])
        self.assertEqual(applied["REOPENED"][0]["LINE"], "000001",
                         "reported against the line it was read on")
        item = self.item_of(invoice["ACCOUNTINGDOCUMENT"])
        self.assertEqual(item["ClearingAccountingDocument"], "",
                         "paid and returned, so open again")
        self.assertTrue(item["ClearingIsReversed"])


class TestFindingTheIDocThatSettledAnInvoice(StatementCase):
    """The join an integration actually has: from the invoice, not to it."""

    def test_the_listing_narrows_to_the_idocs_that_settled_one_document(self):
        paid = self.bill("REV-A1", "1190.00")
        other = self.bill("REV-A2", "2380.00")
        settling = self.send(finsta(
            line("000001", "1190.00-", reference="REV-A1"), statement="00060"))
        self.send(finsta(line("000001", "2380.00-", reference="REV-A2"),
                         statement="00061"))

        _, _, body = self.get("/_mock/idocs?settled=%s"
                              % paid["ACCOUNTINGDOCUMENT"])
        self.assertEqual([r["docnum"] for r in body["results"]],
                         [settling["DOCNUM"]],
                         "that invoice was settled by exactly one IDoc")
        self.assertNotEqual(paid["ACCOUNTINGDOCUMENT"],
                            other["ACCOUNTINGDOCUMENT"])

    def test_an_unsettled_document_matches_nothing(self):
        owed = self.bill("REV-B1", "500.00")
        _, _, body = self.get("/_mock/idocs?settled=%s"
                              % owed["ACCOUNTINGDOCUMENT"])
        self.assertEqual(body["results"], [],
                         "nothing has settled it, so nothing names it")


class TestWhenAPaymentRunHasClaimedAnItem(StatementCase):
    """A claim says an item is *waiting* to be paid, so it goes when it stops.

    `PaymentRunID` keeps a second run from paying an invoice the first one has
    already sent to the bank (#90). What posting a statement has to get right
    is the other end of that: the claim must not survive the item it was made
    about, or an invoice whose payment came back would never be selected
    again - the opposite fault to the one the claim prevents.
    """

    def claim(self, billed, run="F110B"):
        status, _, _ = self.request(
            "PATCH",
            INVOICE_SRV + "/A_SupplierInvoice(SupplierInvoice='%s',FiscalYear='%s')"
            % (billed["SUPPLIERINVOICE"], billed["FISCALYEAR"]),
            body={"PaymentRunID": run, "PaymentRunDate": "2026-10-05"},
            headers=dict(self.csrf_token(), **{"If-Match": "*"}))
        self.assertEqual(status, 204)

    def invoice_of(self, billed):
        _, _, body = self.get(
            INVOICE_SRV + "/A_SupplierInvoice(SupplierInvoice='%s',FiscalYear='%s')"
            "?$format=json" % (billed["SUPPLIERINVOICE"], billed["FISCALYEAR"]))
        return body["d"]

    def test_a_claimed_item_is_still_cleared_by_the_statement(self):
        """The claim stops a *payment run*, not the bank statement.

        Clearing reads the open items directly rather than through a client's
        filter, and it has to: the statement arriving is the whole point of
        having sent the payment, so an item in flight is exactly the one it
        should settle.
        """
        billed = self.bill("CLAIM-1", "1190.00")
        self.claim(billed)

        applied = self.send(finsta(
            line("000001", "1190.00-", reference="CLAIM-1"),
            statement="00110"))["APPLIED"][0]

        self.assertEqual(len(applied["CLEARED"]), 1, applied["UNPROCESSED"])
        self.assertNotIn(billed["ACCOUNTINGDOCUMENT"],
                         [r["AccountingDocument"] for r in self.open_payables()])

    def test_clearing_releases_the_claim_on_both_rows(self):
        """The run that paid it has finished with it, and so have both rows.

        The invoice is checked as well as the item, because a client writes
        the claim on the invoice: one of them still naming a finished run
        would be the two rows disagreeing about one decision.
        """
        billed = self.bill("CLAIM-2", "1190.00")
        self.claim(billed)
        self.assertEqual(self.item_of(billed["ACCOUNTINGDOCUMENT"])
                         ["PaymentRunID"], "F110B")

        self.send(finsta(line("000001", "1190.00-", reference="CLAIM-2"),
                         statement="00111"))

        item = self.item_of(billed["ACCOUNTINGDOCUMENT"])
        self.assertEqual(item["PaymentRunID"], "")
        self.assertIsNone(item["PaymentRunDate"])
        self.assertEqual(self.invoice_of(billed)["PaymentRunID"], "",
                         "the invoice let go too")

    def test_a_returned_payment_releases_the_claim(self):
        """Paid, returned, and payable again - including by a new run.

        If the claim outlived the return, this invoice would sit open forever
        with a finished run's name on it, and every later selection would
        skip it. That is the fault this field exists to prevent, arrived at
        from the other side.
        """
        billed = self.bill("CLAIM-3", "1190.00")
        self.claim(billed)
        self.send(finsta(line("000001", "1190.00-", reference="CLAIM-3"),
                         statement="00112"))
        self.send(finsta(
            line("000001", "1190.00", reference="CLAIM-3", action="RET"),
            statement="00113"))

        item = self.item_of(billed["ACCOUNTINGDOCUMENT"])
        self.assertEqual(item["ClearingAccountingDocument"], "",
                         "open again")
        self.assertTrue(item["ClearingIsReversed"],
                        "and known to have been paid once")
        self.assertEqual(item["PaymentRunID"], "",
                         "so a payment run can select it again")
        self.assertEqual(self.invoice_of(billed)["PaymentRunID"], "")


if __name__ == "__main__":
    unittest.main()
