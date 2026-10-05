"""Posting a bank statement: clearing what it paid, reopening what came back.

The other half of #57. `statement.py` says what the file claims; this is what
the claim does to the open items, which is where it can be expensively wrong.
"""
from __future__ import annotations

import unittest

from support import MockServerCase, balances, finsta, invoic, line

CUBE = ("/sap/opu/odata/sap/API_OPLACCTGDOCITEMCUBE_SRV"
        "/A_OperationalAcctgDocItemCube")


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
        receipt = self.send(finsta(line("000001", "1190.00", reference="OUT-C1"),
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

        Matching still does not check a line's currency against the item's,
        which is #88; this is only that grouping cannot paper over it by
        putting both in one payment.
        """
        self.bill("PAY-H1", "1190.00", supplier="1000009", currency="EUR")
        self.bill("PAY-H2", "2380.00", supplier="1000009", currency="USD")

        applied = self.send(finsta(
            line("000001", "1190.00-", reference="PAY-H1")
            + line("000002", "2380.00-", reference="PAY-H2"),
            statement="00077"))["APPLIED"][0]

        self.assertEqual(len(applied["CLEARED"]), 2)
        self.assertEqual(
            len({row["CLEARINGDOCUMENT"] for row in applied["CLEARED"]}), 2)


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
            line("000001", "1190.00", reference="BACK-A1")      # money in
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


if __name__ == "__main__":
    unittest.main()
