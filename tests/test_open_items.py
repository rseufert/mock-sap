"""Open items: what is still owed, when it fell due, and what cleared it.

The selection a payment run makes - open, due by a date, not blocked, for
this supplier - is one $filter over the open-item cube, so these tests build
a mix of items and assert that the filter returns exactly the right ones.
"""
from __future__ import annotations

import datetime
import unittest
from decimal import Decimal

from support import MockServerCase, SRV

from mocksap import documents  # noqa: E402 - support puts the checkout on the path

CUBE = ("/sap/opu/odata/sap/API_OPLACCTGDOCITEMCUBE_SRV"
        "/A_OperationalAcctgDocItemCube")
JOURNAL = "/sap/opu/odata/sap/API_JOURNALENTRY_SRV"


def day(offset: int) -> str:
    return (datetime.date.today() + datetime.timedelta(days=offset)).isoformat()


def on_the_wire(value):
    """/Date(ms)/ back to an ISO date, without asking the mock how.

    `None` stays `None`: a date nobody is owed is absent rather than epoch.
    """
    if value is None:
        return None
    milliseconds = int(value.strip("/").replace("Date(", "").replace(")", ""))
    return (datetime.datetime(1970, 1, 1)
            + datetime.timedelta(milliseconds=milliseconds)).date().isoformat()


class OpenItemCase(MockServerCase):
    def post(self, supplier="1000001", terms="", baseline=None, block="", amount="100.00"):
        """Post a payable and return (document, company, year)."""
        body = {
            "DOCUMENTHEADER": {"COMP_CODE": "1710", "DOC_TYPE": "KR",
                               "DOC_DATE": day(0), "PSTNG_DATE": day(0)},
            "ACCOUNTPAYABLE": [{"ITEMNO_ACC": "1", "VENDOR_NO": supplier,
                                "PMNTTRMS": terms, "PMTBLOCK": block,
                                "BLINE_DATE": baseline or "", "ITEM_TEXT": "Invoice"}],
            "ACCOUNTGL": [{"ITEMNO_ACC": "2", "GL_ACCOUNT": "0000400000",
                           "ITEM_TEXT": "Expense"}],
            "CURRENCYAMOUNT": [
                {"ITEMNO_ACC": "1", "CURRENCY": "EUR", "AMT_DOCCUR": "-" + amount},
                {"ITEMNO_ACC": "2", "CURRENCY": "EUR", "AMT_DOCCUR": amount}],
        }
        status, _, out = self.request("POST", "/sap/bc/rfc/BAPI_ACC_DOCUMENT_POST",
                                      body=body, headers=self.csrf_token())
        self.assertEqual(status, 200)
        self.assertEqual(out["RETURN"][0]["TYPE"], "S", out["RETURN"])
        key = out["OBJ_KEY"]
        return key[:10], key[10:14], key[14:]

    def supplier_line(self, document):
        """The payable line of a document, read through the cube."""
        _, _, body = self.get(
            CUBE + "?$filter=AccountingDocument eq '%s' and AccountingDocumentItemType eq 'K'"
            "&$format=json" % document)
        results = body["d"]["results"]
        self.assertEqual(len(results), 1, "one payable line per document here")
        return results[0]

    def select(self, clause):
        _, _, body = self.get(CUBE + "?$filter=" + clause.replace(" ", "%20")
                              + "&$format=json")
        return body["d"]["results"]


class TestOpenItemState(OpenItemCase):
    def test_a_payable_is_open_from_the_moment_it_posts(self):
        document, _, _ = self.post(terms="NT30")
        line = self.supplier_line(document)

        self.assertEqual(line["Supplier"], "1000001")
        self.assertEqual(line["DebitCreditCode"], "H", "a payable is a credit")
        self.assertEqual(line["ClearingAccountingDocument"], "",
                         "open means no clearing document, blank not absent")
        self.assertEqual(line["ClearingDate"], None)
        self.assertFalse(line["ClearingIsReversed"])

    def test_the_due_date_follows_from_the_terms(self):
        document, _, _ = self.post(terms="NT30")
        line = self.supplier_line(document)
        # /Date(ms)/ back to a date, so this checks the wire value and not a
        # number the test computed the same way the mock did
        milliseconds = int(line["NetDueDate"].strip("/").replace("Date(", "").replace(")", ""))
        due = datetime.datetime(1970, 1, 1) + datetime.timedelta(milliseconds=milliseconds)
        self.assertEqual(due.date().isoformat(), day(30))

        immediate, _, _ = self.post(terms="0001")
        line = self.supplier_line(immediate)
        milliseconds = int(line["NetDueDate"].strip("/").replace("Date(", "").replace(")", ""))
        due = datetime.datetime(1970, 1, 1) + datetime.timedelta(milliseconds=milliseconds)
        self.assertEqual(due.date().isoformat(), day(0), "0001 is payable at once")

    def test_the_baseline_date_moves_the_due_date(self):
        document, _, _ = self.post(terms="NT30", baseline=day(10))
        line = self.supplier_line(document)
        milliseconds = int(line["NetDueDate"].strip("/").replace("Date(", "").replace(")", ""))
        due = datetime.datetime(1970, 1, 1) + datetime.timedelta(milliseconds=milliseconds)
        self.assertEqual(due.date().isoformat(), day(40),
                         "30 days from the baseline date, not from posting")

    def test_a_gl_line_is_not_an_open_item(self):
        document, _, _ = self.post(terms="NT30")
        _, _, body = self.get(
            CUBE + "?$filter=AccountingDocument eq '%s'&$format=json" % document)
        gl = [r for r in body["d"]["results"] if r["AccountingDocumentItemType"] == "S"]
        self.assertEqual(len(gl), 1)
        self.assertIsNone(gl[0]["NetDueDate"], "nobody is owed a G/L line")
        self.assertEqual(gl[0]["PaymentTerms"], "")


class TestThePaymentRunSelection(OpenItemCase):
    """The whole query a payment run makes, against a mix built on purpose."""

    def setUp(self):
        self.request("POST", "/_mock/reset")
        self.due_now = self.post(terms="0001", amount="100.00")[0]
        self.due_later = self.post(terms="NT60", amount="200.00")[0]
        self.blocked = self.post(terms="0001", block="A", amount="300.00")[0]
        self.other_supplier = self.post(supplier="1000002", terms="0001",
                                        amount="400.00")[0]
        self.cleared = self.post(terms="0001", amount="500.00")[0]
        line = self.supplier_line(self.cleared)
        # The cube is read-only, as the real service is, so a test arranges a
        # cleared item through the mock's own control plane rather than by
        # writing to an entity set no client may write to.
        status, _, _ = self.request("PATCH", "/_mock/open-items", body={
            "AccountingDocument": self.cleared,
            "CompanyCode": line["CompanyCode"],
            "FiscalYear": line["FiscalYear"],
            "AccountingDocumentItem": line["AccountingDocumentItem"],
            "ClearingAccountingDocument": "0100000999",
            "ClearingDate": "2026-09-27",
        })
        self.assertEqual(status, 200)

    def test_open_due_and_not_blocked_for_one_supplier(self):
        rows = self.select(
            "AccountingDocumentItemType eq 'K' and Supplier eq '1000001' "
            "and ClearingAccountingDocument eq '' "
            "and PaymentBlockingReason eq '' "
            "and NetDueDate le datetime'%sT00:00:00'" % day(0))

        documents = sorted(r["AccountingDocument"] for r in rows)
        self.assertEqual(documents, [self.due_now],
                         "not the one due later, the blocked one, the other "
                         "supplier's, or the one already cleared")

    def test_each_exclusion_on_its_own(self):
        supplier = ("AccountingDocumentItemType eq 'K' and Supplier eq '1000001' "
                    "and ClearingAccountingDocument eq ''")

        open_items = [r["AccountingDocument"] for r in self.select(supplier)]
        self.assertIn(self.due_later, open_items, "not yet due is still open")
        self.assertIn(self.blocked, open_items, "blocked is still open")
        self.assertNotIn(self.cleared, open_items, "cleared is not")
        self.assertNotIn(self.other_supplier, open_items)

        blocked = [r["AccountingDocument"] for r in self.select(
            supplier + " and PaymentBlockingReason ne ''")]
        self.assertEqual(blocked, [self.blocked],
                         "a blocked item is visible, which is how a payment "
                         "run reports what it skipped")

    def test_a_cleared_item_says_what_cleared_it(self):
        line = self.supplier_line(self.cleared)
        self.assertEqual(line["ClearingAccountingDocument"], "0100000999")
        self.assertIsNotNone(line["ClearingDate"])

    def test_the_cube_reads_the_journal_entry_s_own_rows(self):
        """One row, two services - so they can never disagree."""
        line = self.supplier_line(self.due_now)
        _, _, entry = self.get(
            JOURNAL + "/A_JournalEntryItem(AccountingDocument='%s',CompanyCode='%s',"
            "FiscalYear='%s',AccountingDocumentItem='%s')?$format=json"
            % (self.due_now, line["CompanyCode"], line["FiscalYear"],
               line["AccountingDocumentItem"]))
        item = entry["d"]

        self.assertEqual(item["Supplier"], line["Supplier"])
        self.assertEqual(item["AmountInTransactionCurrency"],
                         line["AmountInTransactionCurrency"])
        self.assertNotIn("NetDueDate", item,
                         "the journal entry service does not publish open-item "
                         "fields, even though the row carries them")


class TestTheCubeIsAViewNotACopy(OpenItemCase):
    def test_the_cube_has_no_table_of_its_own(self):
        _, _, state = self.get("/_mock/state")
        self.assertIn("A_JournalEntryItem", state)
        self.assertNotIn("A_OperationalAcctgDocItemCube", state,
                         "a view stores nothing, so it counts nothing")

    def test_its_metadata_is_served_like_any_other_service(self):
        status, headers, raw = self.request(
            "GET", "/sap/opu/odata/sap/API_OPLACCTGDOCITEMCUBE_SRV/$metadata", raw=True)
        self.assertEqual(status, 200)
        self.assertIn("xml", headers["Content-Type"])
        document = raw.decode()
        self.assertIn('EntityType Name="A_OperationalAcctgDocItemCubeType"', document)
        self.assertIn('Name="NetDueDate"', document)
        self.assertIn('Name="ClearingAccountingDocument"', document)


class TestTheCubeIsReadOnly(OpenItemCase):
    """The real API_OPLACCTGDOCITEMCUBE_SRV reports; it does not take writes.

    A mock that accepts them lets a client block or clear items a way that
    works here and fails against S/4, which is the one thing a mock must not
    do. Found by mock-bank building a payment run against 0.12.0 (#62).
    """

    def key_of(self, document):
        line = self.supplier_line(document)
        return ("(AccountingDocument='%s',CompanyCode='%s',FiscalYear='%s',"
                "AccountingDocumentItem='%s')"
                % (document, line["CompanyCode"], line["FiscalYear"],
                   line["AccountingDocumentItem"]))

    def test_a_patch_is_refused(self):
        document, _, _ = self.post(terms="NT30")
        status, _, body = self.request(
            "PATCH", CUBE + self.key_of(document),
            body={"PaymentBlockingReason": "A"},
            headers=dict(self.csrf_token(), **{"If-Match": "*"}))

        self.assertEqual(status, 405)
        self.assertIn("read-only", body["error"]["message"]["value"])
        self.assertEqual(self.supplier_line(document)["PaymentBlockingReason"], "",
                         "and nothing was changed")

    def test_a_post_and_a_delete_are_refused(self):
        document, _, _ = self.post(terms="NT30")
        status, _, _ = self.request("POST", CUBE, body={
            "AccountingDocument": "0199999999", "CompanyCode": "1710",
            "FiscalYear": "2026", "AccountingDocumentItem": "000001"},
            headers=self.csrf_token())
        self.assertEqual(status, 405)

        status, _, _ = self.request(
            "DELETE", CUBE + self.key_of(document),
            headers=dict(self.csrf_token(), **{"If-Match": "*"}))
        self.assertEqual(status, 405)

    def test_the_metadata_says_so_too(self):
        status, _, raw = self.request(
            "GET", "/sap/opu/odata/sap/API_OPLACCTGDOCITEMCUBE_SRV/$metadata",
            raw=True)
        self.assertEqual(status, 200)
        document = raw.decode()
        self.assertIn('sap:creatable="false"', document)
        self.assertIn('sap:updatable="false"', document)
        self.assertIn('sap:deletable="false"', document)

    def test_a_writable_service_still_says_it_is_writable(self):
        _, _, raw = self.request(
            "GET", "/sap/opu/odata/sap/API_JOURNALENTRY_SRV/$metadata", raw=True)
        self.assertIn('sap:creatable="true"', raw.decode(),
                      "read-only is a property of this service, not of all of them")

    def test_reading_still_works(self):
        document, _, _ = self.post(terms="NT30")
        line = self.supplier_line(document)
        self.assertEqual(line["Supplier"], "1000001")


class TestTermsThatCarryADiscount(OpenItemCase):
    """A terms key that offers a cash discount, and what a payment earns of it.

    The net half is on the wire, so it is read back off the cube. The discount
    half is not: nothing serves a document's discount terms today, and until
    `reconcile.py` judges a short payment by them (#65) the only client is
    Python. So these go at the figures directly, and the figures are spelled
    out rather than computed the way the mock computes them - a test that
    multiplied by 2% would agree with the code whatever the code did.
    """

    def test_a_discount_key_is_an_ordinary_terms_key_on_the_wire(self):
        document, _, _ = self.post(terms="0002")
        line = self.supplier_line(document)
        milliseconds = int(line["NetDueDate"].strip("/").replace("Date(", "").replace(")", ""))
        due = datetime.datetime(1970, 1, 1) + datetime.timedelta(milliseconds=milliseconds)
        self.assertEqual(due.date().isoformat(), day(30),
                         "2% 10 net 30 falls due in 30 days like any net-30 key")
        self.assertEqual(line["PaymentTerms"], "0002",
                         "the key is carried as it arrived, four characters of it")

    def test_the_keys_that_existed_before_answer_as_they_did(self):
        self.assertEqual(documents.net_due_date("20260930", ""), "2026-09-30")
        self.assertEqual(documents.net_due_date("20260930", "0001"), "2026-09-30")
        self.assertEqual(documents.net_due_date("20260930", "NT30"), "2026-10-30")
        self.assertEqual(documents.net_due_date("20260930", "NT45"), "2026-11-14")
        self.assertEqual(documents.net_due_date("20260930", "NT60"), "2026-11-29")
        self.assertEqual(documents.net_due_date("20260930", "ZZZZ"), "2026-09-30",
                         "a key nobody configured is due at once, not refused")
        self.assertEqual(documents.net_due_date("20260930", "NT30", days=7),
                         "2026-10-07", "the document's own days still win")
        for key in ("", "0001", "NT30", "NT45", "NT60"):
            terms = documents.terms_of(key)
            self.assertEqual(terms.discount_days, 0, key)
            self.assertEqual(terms.discount_percent, Decimal("0"),
                             "%s never offered a discount" % key)

    def test_a_payment_inside_the_window_earns_the_discount(self):
        # 2% 10 net 30 on 1,250, paid on the tenth day
        earned, available = documents.discount_on(
            "0002", "2026-09-30", "2026-10-10", "1250.00")
        self.assertEqual(earned, Decimal("25.00"))
        self.assertEqual(available, Decimal("25.00"))

    def test_a_day_late_earns_nothing_and_still_says_what_was_offered(self):
        earned, available = documents.discount_on(
            "0002", "2026-09-30", "2026-10-11", "1250.00")
        self.assertEqual(earned, Decimal("0.00"), "the window closed yesterday")
        self.assertEqual(available, Decimal("25.00"))
        self.assertEqual(available - earned, Decimal("25.00"),
                         "what stays open when the invoice itself clears")

    def test_paying_early_is_paying_in_time(self):
        discount = documents.discount_on(
            "0002", "2026-09-30", "2026-09-01", "1250.00")
        self.assertEqual(discount.earned, Decimal("25.00"))

    def test_terms_with_no_discount_offer_nothing_to_earn(self):
        self.assertEqual(
            documents.discount_on("NT30", "2026-09-30", "2026-10-01", "1250.00"),
            (Decimal("0.00"), Decimal("0.00")))
        self.assertEqual(
            documents.discount_on("", "2026-09-30", "2026-09-30", "1250.00"),
            (Decimal("0.00"), Decimal("0.00")))
        self.assertEqual(
            documents.discount_on("0002", "2026-09-30", "2026-10-01", "1250.00",
                                  percent="0"),
            (Decimal("0.00"), Decimal("0.00")),
            "a window on nothing is not a discount")

    def test_the_discount_is_rounded_up_at_a_half_cent(self):
        # 2% of 1.25 is 0.025 exactly: half up is 0.03, and the rounding
        # Python's own round() would do is 0.02
        discount = documents.discount_on(
            "0002", "2026-09-30", "2026-10-01", "1.25")
        self.assertEqual(discount.available, Decimal("0.03"))
        self.assertEqual(str(discount.available), "0.03", "two places, kept")

    def test_a_scale_the_caller_asks_for_is_the_scale_it_gets(self):
        discount = documents.discount_on(
            "0002", "2026-09-30", "2026-10-01", "1250.00", scale=3)
        self.assertEqual(str(discount.available), "25.000",
                         "the open-item cube declares three places")

    def test_a_document_s_own_figures_win_over_its_terms(self):
        earned, available = documents.discount_on(
            "NT30", "2026-09-30", "2026-10-03", "1000.00",
            days=5, percent="2.5")
        self.assertEqual(available, Decimal("25.00"))
        self.assertEqual(earned, Decimal("25.00"))
        self.assertEqual(
            documents.discount_on("NT30", "2026-09-30", "2026-10-06", "1000.00",
                                  days=5, percent="2.5").earned,
            Decimal("0.00"), "five days from the baseline, not thirty")

    def test_the_sign_is_the_caller_s(self):
        discount = documents.discount_on(
            "0002", "2026-09-30", "2026-10-01", "-1250.00")
        self.assertEqual(discount.available, Decimal("-25.00"),
                         "which way the money goes is not this function's question")

    def test_the_last_day_of_the_discount_is_a_date_or_nothing(self):
        self.assertEqual(documents.discount_due_date("2026-09-30", "0002"),
                         "2026-10-10")
        self.assertEqual(documents.discount_due_date("2026-09-30", "0003"),
                         "2026-10-14")
        self.assertIsNone(documents.discount_due_date("2026-09-30", "NT30"),
                          "no discount is None, not the baseline date")
        self.assertIsNone(documents.discount_due_date("2026-09-30", "ZZZZ"))


class TestWhatADueDateWasCountedFrom(OpenItemCase):
    """An open item serves its baseline date, not only the date it falls due.

    Subtracting the terms' days back off `NetDueDate` is not the same fact: a
    document that carried its own `NetPaymentDays` - a supplier invoice does -
    has a due date the terms cannot explain, and anything judging what the
    terms say beyond the net date needs the date they were counted from (#182).
    """

    def test_a_payable_serves_the_baseline_it_was_given(self):
        document, _, _ = self.post(terms="NT30", baseline=day(10))
        line = self.supplier_line(document)
        self.assertEqual(on_the_wire(line["DueCalculationBaseDate"]), day(10))
        self.assertEqual(on_the_wire(line["NetDueDate"]), day(40))

    def test_an_item_with_no_baseline_was_counted_from_the_posting(self):
        document, _, _ = self.post(terms="NT30")
        line = self.supplier_line(document)
        self.assertEqual(on_the_wire(line["DueCalculationBaseDate"]), day(0))

    def test_a_gl_line_was_counted_from_nothing(self):
        document, _, _ = self.post(terms="NT30")
        _, _, body = self.get(
            CUBE + "?$filter=AccountingDocument eq '%s'&$format=json" % document)
        gl = [r for r in body["d"]["results"] if r["AccountingDocumentItemType"] == "S"]
        self.assertEqual(len(gl), 1)
        self.assertIsNone(gl[0]["DueCalculationBaseDate"],
                          "nobody is owed a G/L line, so nothing was counted")

    def test_the_metadata_names_it(self):
        _, _, raw = self.request(
            "GET", "/sap/opu/odata/sap/API_OPLACCTGDOCITEMCUBE_SRV/$metadata",
            raw=True)
        self.assertIn('Name="DueCalculationBaseDate"', raw.decode())

    def test_the_control_plane_can_arrange_one(self):
        document, _, _ = self.post(terms="NT30")
        line = self.supplier_line(document)
        status, _, _ = self.request("PATCH", "/_mock/open-items", body={
            "AccountingDocument": document,
            "CompanyCode": line["CompanyCode"],
            "FiscalYear": line["FiscalYear"],
            "AccountingDocumentItem": line["AccountingDocumentItem"],
            "DueCalculationBaseDate": "2026-09-01",
        })
        self.assertEqual(status, 200)
        self.assertEqual(
            on_the_wire(self.supplier_line(document)["DueCalculationBaseDate"]),
            "2026-09-01", "a test can arrange a baseline without posting one")

    def test_the_seeded_receivables_agree_with_their_own_terms(self):
        rows = self.select("AccountingDocumentItemType eq 'D'")
        self.assertTrue(rows, "the seed should contain receivables")
        for row in rows:
            baseline = on_the_wire(row["DueCalculationBaseDate"])
            due = on_the_wire(row["NetDueDate"])
            self.assertEqual(row["PaymentTerms"], "NT30", row["AccountingDocument"])
            self.assertEqual(
                (datetime.date.fromisoformat(due)
                 - datetime.date.fromisoformat(baseline)).days, 30,
                "NT30 means thirty days from the baseline, in the seed too")


class TestAReceivableCarriesTheTermsWeQuoted(OpenItemCase):
    """The invoice we send and the open item we keep say the same thing.

    The outbound INVOIC quotes the order's `CustomerPaymentTerms` as ZTERM.
    Until #182 the receivable behind it carried no terms at all and fell due
    on the day it posted, so a customer promised 45 days was already overdue
    in our own books the moment we invoiced them.
    """

    def an_order_due_in(self, terms):
        _, _, created = self.request(
            "POST", SRV + "/A_SalesOrder?$expand=to_Item",
            headers=self.csrf_token(), body={
                "SalesOrderType": "OR", "SalesOrganization": "1710",
                "SoldToParty": "1000001", "DistributionChannel": "10",
                "OrganizationDivision": "00", "TransactionCurrency": "EUR",
                "CustomerPaymentTerms": terms,
                "to_Item": [{"Material": "TG11", "RequestedQuantity": "4",
                             "RequestedQuantityUnit": "PC",
                             "NetAmount": "400.00"}]})
        return created["d"]

    def customer_line(self, document):
        rows = self.select("AccountingDocument eq '%s' and "
                           "AccountingDocumentItemType eq 'D'" % document)
        self.assertEqual(len(rows), 1, "one receivable per billing document")
        return rows[0]

    def test_the_receivable_falls_due_when_the_invoice_says_it_does(self):
        order = self.an_order_due_in("NT45")
        status, _, generated = self.request(
            "POST", "/sap/bc/idoc/generate",
            body={"mestyp": "INVOIC", "SalesOrder": order["SalesOrder"]},
            headers=self.csrf_token())
        self.assertEqual(status, 201)
        self.assertIn("<ZTERM>NT45</ZTERM>", generated["xml"],
                      "the customer is told 45 days")

        line = self.customer_line(generated["accounting_document"])
        self.assertEqual(line["PaymentTerms"], "NT45",
                         "and our own books say the same")
        self.assertEqual(on_the_wire(line["DueCalculationBaseDate"]), day(0))
        self.assertEqual(on_the_wire(line["NetDueDate"]), day(45),
                         "not the posting date, which is what it used to be")

    def test_an_order_with_no_terms_is_still_due_at_once(self):
        order = self.an_order_due_in("")
        _, _, generated = self.request(
            "POST", "/sap/bc/idoc/generate",
            body={"mestyp": "INVOIC", "SalesOrder": order["SalesOrder"]},
            headers=self.csrf_token())
        line = self.customer_line(generated["accounting_document"])
        self.assertEqual(line["PaymentTerms"], "")
        self.assertEqual(on_the_wire(line["NetDueDate"]), day(0),
                         "a blank ZTERM is payable at once, as it was")


if __name__ == "__main__":
    unittest.main()
