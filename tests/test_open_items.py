"""Open items: what is still owed, when it fell due, and what cleared it.

The selection a payment run makes - open, due by a date, not blocked, for
this supplier - is one $filter over the open-item cube, so these tests build
a mix of items and assert that the filter returns exactly the right ones.
"""
from __future__ import annotations

import datetime
import unittest

from support import MockServerCase

CUBE = ("/sap/opu/odata/sap/API_OPLACCTGDOCITEMCUBE_SRV"
        "/A_OperationalAcctgDocItemCube")
JOURNAL = "/sap/opu/odata/sap/API_JOURNALENTRY_SRV"


def day(offset: int) -> str:
    return (datetime.date.today() + datetime.timedelta(days=offset)).isoformat()


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


if __name__ == "__main__":
    unittest.main()


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
