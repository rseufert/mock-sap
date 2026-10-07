"""A customer's payment on the statement clears the receivable it quotes (#175).

The mirror of `test_statement_posting.py`. A debit clears a payable; a credit
that says it is money arriving clears a receivable, on the same terms - the
reference, the amount and its currency all have to agree. What differs is who
chose the reference: we chose the one a supplier is paid under, and a customer
chooses their own, so the cases worth holding are the ones where they got it
nearly right.

The receivables are the seed's. Customer 1000006 owes two billing documents,
which is what makes one customer paying two invoices testable without building
an order first.
"""
from __future__ import annotations

import unittest
from decimal import Decimal

from support import MockServerCase, finsta, invoic, line

CUBE = ("/sap/opu/odata/sap/API_OPLACCTGDOCITEMCUBE_SRV"
        "/A_OperationalAcctgDocItemCube")
JOURNAL = "/sap/opu/odata/sap/API_JOURNALENTRY_SRV"
ORDERS = "/sap/opu/odata/sap/API_SALES_ORDER_SRV"

# billing document -> (accounting document, what is owed, who owes it)
SMALL = ("0090000006", "0100000006", "8533.25", "1000006")
LARGE = ("0090000001", "0100000001", "29496.20", "1000006")
OTHER = ("0090000002", "0100000002", "65666.85", "1000008")


class ReceivableCase(MockServerCase):
    def setUp(self):
        # Every test here clears one of the same few seeded receivables, and
        # the server is one per class, so each starts from the seed again.
        status, _, _ = self.request("POST", "/_mock/reset")
        self.assertEqual(status, 200)

    def send(self, body):
        headers = dict(self.csrf_token(), **{"Content-Type": "application/xml",
                                             "Accept": "application/json"})
        status, _, receipt = self.request("POST", "/sap/bc/idoc", body=body,
                                          headers=headers)
        self.assertEqual(status, 201)
        return receipt

    def post(self, *lines, **kwargs):
        return self.send(finsta("".join(lines), **kwargs))["APPLIED"][0]

    def items(self, item_type, open_only=True):
        query = "AccountingDocumentItemType%%20eq%%20'%s'" % item_type
        if open_only:
            query += "%20and%20ClearingAccountingDocument%20eq%20''"
        _, _, body = self.get(CUBE + "?$filter=" + query + "&$format=json")
        return body["d"]["results"]

    def open_receivables(self):
        return [row["AccountingDocument"] for row in self.items("D")]

    def receivable(self, accounting_document):
        return [row for row in self.items("D", open_only=False)
                if row["AccountingDocument"] == accounting_document][0]

    def billed(self, net, customer="1000001"):
        """An order billed for `net` plus tax: (billing document, accounting
        document, what is owed). The seed's amounts are what they are, and a
        test about sums has to choose its own."""
        _, _, order = self.request(
            "POST", ORDERS + "/A_SalesOrder?$expand=to_Item",
            headers=self.csrf_token(), body={
                "SalesOrderType": "OR", "SalesOrganization": "1710",
                "SoldToParty": customer, "DistributionChannel": "10",
                "OrganizationDivision": "00", "TransactionCurrency": "EUR",
                "to_Item": [{"Material": "TG11", "RequestedQuantity": "1",
                             "RequestedQuantityUnit": "PC",
                             "NetAmount": net}]})
        status, _, generated = self.request(
            "POST", "/sap/bc/idoc/generate",
            body={"mestyp": "INVOIC", "SalesOrder": order["d"]["SalesOrder"]},
            headers=self.csrf_token())
        self.assertEqual(status, 201)
        document = generated["accounting_document"]
        owed = Decimal(self.receivable(document)["AmountInTransactionCurrency"])
        return generated["billing_document"], document, "%.2f" % owed

    def entry(self, document):
        _, _, body = self.get(
            JOURNAL + "/A_JournalEntry?$filter=AccountingDocument%%20eq%%20"
            "'%s'&$expand=to_JournalEntryItem&$format=json" % document)
        results = body["d"]["results"]
        self.assertEqual(len(results), 1, "one document for that number")
        return results[0]


class TestACustomerPaying(ReceivableCase):
    def test_a_credit_for_the_exact_amount_clears_the_receivable(self):
        billing, document, owed, _ = SMALL
        before = self.open_receivables()

        applied = self.post(line("000001", owed, reference=billing,
                                 action="RCV"), statement="00201")

        self.assertEqual(applied["UNPROCESSED"], [])
        self.assertEqual(len(applied["CLEARED"]), 1)
        cleared = applied["CLEARED"][0]
        self.assertEqual((cleared["REFERENCE"], cleared["ACCOUNTINGDOCUMENT"]),
                         (billing, document))
        self.assertEqual(Decimal(cleared["AMOUNT"]), Decimal(owed))
        self.assertEqual(sorted(self.open_receivables()),
                         sorted(set(before) - {document}),
                         "that receivable stops being open and no other does, "
                         "and the receipt leaves no open line of its own")

    def test_the_item_says_what_cleared_it_and_when(self):
        billing, document, owed, _ = SMALL
        applied = self.post(line("000001", owed, reference=billing,
                                 action="RCV"),
                            statement="00202", date="20260927")

        item = self.receivable(document)

        self.assertEqual(item["ClearingAccountingDocument"],
                         applied["CLEARED"][0]["CLEARINGDOCUMENT"])
        self.assertEqual(item["ClearingItem"], "000001")
        self.assertIsNotNone(item["ClearingDate"])
        self.assertEqual(item["ClearingDate"], item["ClearingCreationDate"])
        self.assertFalse(item["ClearingIsReversed"])

    def test_the_bank_gains_the_money_and_the_customer_stops_owing_it(self):
        """The signs are the whole difference from a payment of ours.

        Posted with a payment's signs, a receipt takes the money out of the
        bank and doubles what the customer owes, and still clears the item.
        """
        billing, document, owed, customer = SMALL
        applied = self.post(line("000001", owed, reference=billing,
                                 action="RCV"), statement="00203")

        entry = self.entry(applied["CLEARED"][0]["CLEARINGDOCUMENT"])
        lines = entry["to_JournalEntryItem"]["results"]
        customers = [x for x in lines if x["Customer"]]
        bank = [x for x in lines if not x["Customer"]]

        self.assertEqual(entry["AccountingDocumentType"], "DZ")
        self.assertEqual(entry["ReferenceDocument"], "00203")
        self.assertEqual([x["Customer"] for x in customers], [customer])
        self.assertEqual(Decimal(customers[0]["AmountInTransactionCurrency"]),
                         Decimal(owed))
        self.assertEqual(customers[0]["DebitCreditCode"], "H",
                         "a credit on the customer's account")
        self.assertEqual(len(bank), 1)
        self.assertEqual(Decimal(bank[0]["AmountInTransactionCurrency"]),
                         Decimal(owed))
        self.assertEqual(bank[0]["DebitCreditCode"], "S",
                         "a debit to the bank: money in")
        self.assertFalse(any(x["Supplier"] for x in lines))

    def test_a_reference_only_in_the_note_still_matches(self):
        billing, document, owed, _ = SMALL
        applied = self.post(
            line("000001", owed, note="Payment for invoice %s thank you"
                 % billing, action="RCV"), statement="00204")

        self.assertEqual([row["ACCOUNTINGDOCUMENT"]
                          for row in applied["CLEARED"]], [document])

    def test_a_reference_the_bank_wrapped_mid_number_still_matches(self):
        billing, document, owed, _ = SMALL
        note = "x" * 65 + billing            # the 70-character wrap splits it
        applied = self.post(line("000001", owed, note=note, action="RCV"),
                            statement="00205")

        self.assertEqual([row["ACCOUNTINGDOCUMENT"]
                          for row in applied["CLEARED"]], [document])

    def test_the_outcome_is_read_back_from_the_idoc(self):
        billing, _, owed, _ = SMALL
        receipt = self.send(finsta(line("000001", owed, reference=billing,
                                        action="RCV"), statement="00206"))

        _, _, read = self.get("/sap/bc/idoc/%s" % receipt["DOCNUM"])

        self.assertEqual(read["APPLIED"], receipt["APPLIED"])


class TestWhatACustomerGotNearlyRight(ReceivableCase):
    """A part payment is refused, not posted: no residual item, no partial
    payment. The line is reported with both figures and nothing moves."""

    def refused(self, amount, statement, currency="EUR"):
        billing, document, _, _ = SMALL
        before = self.items("D", open_only=False)
        applied = self.post(line("000001", amount, reference=billing,
                                 action="RCV", currency=currency),
                            statement=statement)
        self.assertEqual(applied["CLEARED"], [])
        self.assertEqual(len(applied["UNPROCESSED"]), 1)
        self.assertIn(document, self.open_receivables())
        self.assertEqual(len(self.items("D", open_only=False)), len(before),
                         "no residual item and no part payment was posted")
        return applied["UNPROCESSED"][0]["REASON"]

    def test_a_short_payment_clears_nothing_and_names_both_figures(self):
        reason = self.refused("8000.00", "00211")

        self.assertIn("8533.25 EUR", reason)
        self.assertIn("8000.00 EUR", reason)

    def test_an_overpayment_clears_nothing_either(self):
        reason = self.refused("9000.00", "00212")

        self.assertIn("8533.25 EUR", reason)
        self.assertIn("9000.00 EUR", reason)

    def test_the_right_number_in_the_wrong_currency_is_not_a_payment(self):
        reason = self.refused("8533.25", "00213", currency="USD")

        self.assertIn("8533.25 USD", reason)
        self.assertIn("8533.25 EUR", reason)

    def test_money_arriving_that_quotes_nothing_owed_is_reported(self):
        """Never dropped. An unapplied credit that vanishes is the worst
        outcome and the easiest one to write."""
        before = self.open_receivables()
        applied = self.post(line("000001", "500.00", reference="0099999999",
                                 action="RCV"), statement="00214")

        self.assertEqual(applied["CLEARED"], [])
        self.assertEqual([row["LINE"] for row in applied["UNPROCESSED"]],
                         ["000001"])
        self.assertIn("applied to nothing", applied["UNPROCESSED"][0]["REASON"])
        self.assertEqual(self.open_receivables(), before)

    def test_one_receivable_is_not_paid_twice_by_one_statement(self):
        billing, document, owed, _ = SMALL
        applied = self.post(
            line("000001", owed, reference=billing, action="RCV"),
            line("000002", owed, reference=billing, action="RCV"),
            statement="00215")

        self.assertEqual([row["LINE"] for row in applied["CLEARED"]],
                         ["000001"])
        self.assertEqual([row["LINE"] for row in applied["UNPROCESSED"]],
                         ["000002"], "the second is money nobody owed")


class TestWhichWayIsACredit(ReceivableCase):
    """The side and the kind decide which ledger a line can touch, and the
    reference never does. Getting this backwards clears the wrong things
    without a word."""

    def test_money_out_never_clears_a_receivable(self):
        billing, document, owed, _ = SMALL
        applied = self.post(line("000001", owed + "-", reference=billing),
                            statement="00221")

        self.assertEqual(applied["CLEARED"], [])
        self.assertIn(document, self.open_receivables())

    def test_a_credit_that_does_not_say_which_kind_clears_nothing(self):
        billing, document, owed, _ = SMALL
        applied = self.post(line("000001", owed, reference=billing),
                            statement="00222")

        self.assertEqual(applied["CLEARED"], [])
        self.assertIn("does not say which kind",
                      applied["UNPROCESSED"][0]["REASON"])
        self.assertIn(document, self.open_receivables())

    def test_a_payment_coming_back_clears_no_receivable(self):
        billing, document, owed, _ = SMALL
        applied = self.post(line("000001", owed, reference=billing,
                                 action="RET"), statement="00223")

        self.assertEqual((applied["CLEARED"], applied["REOPENED"]), ([], []))
        self.assertIn(document, self.open_receivables())

    def test_a_payment_coming_back_reopens_no_receivable(self):
        billing, document, owed, _ = SMALL
        self.post(line("000001", owed, reference=billing, action="RCV"),
                  statement="00224")

        applied = self.post(line("000001", owed, reference=billing,
                                 action="RET"), statement="00225")

        self.assertEqual(applied["REOPENED"], [])
        self.assertNotIn(document, self.open_receivables())

    def test_money_arriving_never_clears_a_payable(self):
        """A supplier's refund quoting their own invoice is not us paying it."""
        bill = self.send(invoic("REFUND-1", "1190.00", "1000.00", "190.00",
                                "1000009", "EUR"))["APPLIED"][0]
        applied = self.post(line("000001", "1190.00", reference="REFUND-1",
                                 action="RCV"), statement="00226")

        self.assertEqual(applied["CLEARED"], [])
        self.assertIn(bill["ACCOUNTINGDOCUMENT"],
                      [row["AccountingDocument"] for row in self.items("K")])

    def test_a_statement_that_pays_and_is_paid_does_both(self):
        """One document cannot be a payment and a receipt, so there are two."""
        bill = self.send(invoic("BOTH-1", "1190.00", "1000.00", "190.00",
                                "1000009", "EUR"))["APPLIED"][0]
        billing, document, owed, _ = SMALL

        applied = self.post(
            line("000001", owed, reference=billing, action="RCV"),
            line("000002", "1190.00-", reference="BOTH-1"),
            statement="00227")

        self.assertEqual([row["ACCOUNTINGDOCUMENT"] for row in applied["CLEARED"]],
                         [document, bill["ACCOUNTINGDOCUMENT"]],
                         "both cleared, reported in the statement's order")
        receipt, payment = [self.entry(row["CLEARINGDOCUMENT"])
                            for row in applied["CLEARED"]]
        self.assertEqual((receipt["AccountingDocumentType"],
                          payment["AccountingDocumentType"]), ("DZ", "ZP"))
        self.assertEqual(self.items("K"), [], "the payment left no payable")
        self.assertNotIn(document, self.open_receivables())


class TestOneReceiptPerCustomer(ReceivableCase):
    def test_two_invoices_of_one_customer_share_one_receipt(self):
        applied = self.post(
            line("000001", SMALL[2], reference=SMALL[0], action="RCV"),
            line("000002", LARGE[2], reference=LARGE[0], action="RCV"),
            statement="00231")

        documents = {row["CLEARINGDOCUMENT"] for row in applied["CLEARED"]}
        self.assertEqual(len(applied["CLEARED"]), 2)
        self.assertEqual(len(documents), 1, "one customer, one receipt")
        self.assertEqual([self.receivable(x[1])["ClearingItem"]
                          for x in (SMALL, LARGE)], ["000001", "000002"],
                         "each invoice points at its own line of the receipt")
        entry = self.entry(documents.pop())
        bank = [x for x in entry["to_JournalEntryItem"]["results"]
                if not x["Customer"]]
        self.assertEqual(Decimal(bank[0]["AmountInTransactionCurrency"]),
                         Decimal(SMALL[2]) + Decimal(LARGE[2]))

    def test_two_customers_get_a_receipt_each(self):
        applied = self.post(
            line("000001", SMALL[2], reference=SMALL[0], action="RCV"),
            line("000002", OTHER[2], reference=OTHER[0], action="RCV"),
            statement="00232")

        documents = [row["CLEARINGDOCUMENT"] for row in applied["CLEARED"]]
        self.assertEqual(len(set(documents)), 2)
        for document, (_, _, _, customer) in zip(documents, (SMALL, OTHER)):
            lines = self.entry(document)["to_JournalEntryItem"]["results"]
            self.assertEqual({x["Customer"] for x in lines if x["Customer"]},
                             {customer})


class TestOneCreditForSeveralInvoices(ReceivableCase):
    """A customer pays three invoices with one transfer: the line quotes each
    and carries the total (#178)."""

    def test_a_credit_for_the_sum_of_what_it_quotes_clears_all_of_it(self):
        total = "%.2f" % (Decimal(SMALL[2]) + Decimal(LARGE[2]))
        before = self.open_receivables()

        applied = self.post(
            line("000001", total, note="%s %s" % (SMALL[0], LARGE[0]),
                 action="RCV"), statement="00241")

        self.assertEqual(applied["UNPROCESSED"], [])
        self.assertEqual(sorted((row["LINE"], row["ACCOUNTINGDOCUMENT"],
                                 Decimal(row["AMOUNT"]))
                                for row in applied["CLEARED"]),
                         sorted([("000001", SMALL[1], Decimal(SMALL[2])),
                                 ("000001", LARGE[1], Decimal(LARGE[2]))]),
                         "a row per invoice, each for what that invoice was")
        self.assertEqual(sorted(self.open_receivables()),
                         sorted(set(before) - {SMALL[1], LARGE[1]}))

    def test_the_receipt_has_a_line_per_invoice_and_the_bank_gets_the_sum(self):
        total = Decimal(SMALL[2]) + Decimal(LARGE[2])
        applied = self.post(
            line("000001", "%.2f" % total,
                 note="%s %s" % (SMALL[0], LARGE[0]), action="RCV"),
            statement="00242")

        documents = {row["CLEARINGDOCUMENT"] for row in applied["CLEARED"]}
        self.assertEqual(len(documents), 1)
        lines = self.entry(documents.pop())["to_JournalEntryItem"]["results"]
        self.assertEqual(sorted(Decimal(x["AmountInTransactionCurrency"])
                                for x in lines if x["Customer"]),
                         sorted([Decimal(SMALL[2]), Decimal(LARGE[2])]),
                         "each invoice is credited what it was for, not the "
                         "line's amount")
        self.assertEqual([Decimal(x["AmountInTransactionCurrency"])
                          for x in lines if not x["Customer"]], [total])
        self.assertEqual(sorted(self.receivable(x[1])["ClearingItem"]
                                for x in (SMALL, LARGE)), ["000001", "000002"])

    def test_quoting_two_and_paying_for_one_clears_that_one(self):
        applied = self.post(
            line("000001", SMALL[2], note="%s %s" % (SMALL[0], LARGE[0]),
                 action="RCV"), statement="00243")

        self.assertEqual([row["ACCOUNTINGDOCUMENT"]
                          for row in applied["CLEARED"]], [SMALL[1]])
        self.assertIn(LARGE[1], self.open_receivables())

    def test_only_what_the_line_quotes_is_considered(self):
        """The customer's other invoice would make up the sum, and the line
        does not mention it."""
        total = "%.2f" % (Decimal(SMALL[2]) + Decimal(LARGE[2]))
        before = self.open_receivables()

        applied = self.post(line("000001", total, reference=SMALL[0],
                                 action="RCV"), statement="00244")

        self.assertEqual(applied["CLEARED"], [])
        self.assertEqual(self.open_receivables(), before)
        self.assertIn("%s EUR more than" % LARGE[2],
                      applied["UNPROCESSED"][0]["REASON"])

    def test_two_sets_that_both_fit_clear_neither_and_are_both_named(self):
        """100, 200 and 300 net: the third, or the first two."""
        one, two, three = (self.billed(net) for net in
                           ("100.00", "200.00", "300.00"))
        self.assertEqual(Decimal(one[2]) + Decimal(two[2]), Decimal(three[2]),
                         "the test's own premise")
        before = self.open_receivables()

        applied = self.post(
            line("000001", three[2],
                 note=" ".join(x[0] for x in (one, two, three)), action="RCV"),
            statement="00245")

        self.assertEqual(applied["CLEARED"], [])
        self.assertEqual(self.open_receivables(), before)
        reason = applied["UNPROCESSED"][0]["REASON"]
        for _, document, _ in (one, two, three):
            self.assertIn(document, reason)
        self.assertIn("nothing on the line says which", reason)

    def test_the_sets_are_named_as_sets(self):
        one, two, three = (self.billed(net) for net in
                           ("100.00", "200.00", "300.00"))
        reason = self.post(
            line("000001", three[2],
                 note=" ".join(x[0] for x in (one, two, three)), action="RCV"),
            statement="00246")["UNPROCESSED"][0]["REASON"]

        together, alone = reason.split(" or ")
        if three[1] in together:
            together, alone = alone, together
        self.assertIn(one[1], together)
        self.assertIn(two[1], together)
        self.assertNotIn(three[1], together)
        self.assertNotIn(one[1], alone)

    def test_a_sum_no_set_comes_to_says_how_far_off_it_is(self):
        total = Decimal(SMALL[2]) + Decimal(LARGE[2])
        short = self.post(
            line("000001", "%.2f" % (total - Decimal("100.00")),
                 note="%s %s" % (SMALL[0], LARGE[0]), action="RCV"),
            statement="00247")
        over = self.post(
            line("000001", "%.2f" % (total + Decimal("50.00")),
                 note="%s %s" % (SMALL[0], LARGE[0]), action="RCV"),
            statement="00248")

        self.assertEqual((short["CLEARED"], over["CLEARED"]), ([], []))
        self.assertIn("100.00 EUR short of", short["UNPROCESSED"][0]["REASON"])
        self.assertIn("%.2f EUR together" % total,
                      short["UNPROCESSED"][0]["REASON"])
        self.assertIn("50.00 EUR more than", over["UNPROCESSED"][0]["REASON"])

    def test_a_single_short_payment_says_how_short(self):
        applied = self.post(line("000001", "8000.00", reference=SMALL[0],
                                 action="RCV"), statement="00249")

        self.assertIn("533.25 EUR short of",
                      applied["UNPROCESSED"][0]["REASON"])

    def test_more_invoices_than_it_will_search_are_refused_by_number(self):
        """Seventeen invoices are 131,071 sets. It says so and stops, even
        here, where all of them together are exactly what arrived."""
        bills = [self.billed("10.00") for _ in range(17)]
        total = "%.2f" % sum(Decimal(owed) for _, _, owed in bills)

        applied = self.post(
            line("000001", total, note=" ".join(b[0] for b in bills),
                 action="RCV"), statement="00251")

        self.assertEqual(applied["CLEARED"], [])
        reason = applied["UNPROCESSED"][0]["REASON"]
        self.assertIn("17 open receivables", reason)
        self.assertIn("16", reason)

    def test_as_many_as_it_will_search_are_cleared(self):
        bills = [self.billed("10.00") for _ in range(2)]
        total = "%.2f" % sum(Decimal(owed) for _, _, owed in bills)

        applied = self.post(
            line("000001", total, note=" ".join(b[0] for b in bills),
                 action="RCV"), statement="00252")

        self.assertEqual(len(applied["CLEARED"]), 2)

    def test_an_order_billed_and_paid_is_cleared(self):
        """Order to cash, the half no test had driven: bill it, be paid."""
        billing, document, owed = self.billed("400.00")

        applied = self.post(line("000001", owed, reference=billing,
                                 action="RCV"), statement="00250")

        self.assertEqual([row["ACCOUNTINGDOCUMENT"]
                          for row in applied["CLEARED"]], [document])
        self.assertNotIn(document, self.open_receivables())


if __name__ == "__main__":
    unittest.main()
