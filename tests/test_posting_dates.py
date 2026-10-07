"""A date a caller sent: read the same on every Python, or refused in words.

`BAPI_ACC_DOCUMENT_POST` took its dates as they came. One it could not read
was a 500 with the document half written, where every other business refusal
here is a `RETURN` row of type `E`; and SAP's own `YYYYMMDD` was read by
`date.fromisoformat`, which learned that shape in Python 3.11, so the same
call posted on one interpreter and failed on another (#94).

The answers below are spelled out rather than derived, on purpose: a test that
asked the interpreter what `20260930` means would pass wherever it ran and pin
nothing. CI runs these on every supported Python, and they must say the same
thing on each.
"""
from __future__ import annotations

import datetime
import unittest

from support import MockServerCase

from mocksap import documents  # noqa: E402 - support puts the checkout on the path
from mocksap.service import Context  # noqa: E402

CUBE = ("/sap/opu/odata/sap/API_OPLACCTGDOCITEMCUBE_SRV"
        "/A_OperationalAcctgDocItemCube")
JOURNAL = "/sap/opu/odata/sap/API_JOURNALENTRY_SRV"


def on_the_wire(value):
    """/Date(ms)/ back to an ISO date, without asking the mock how."""
    milliseconds = int(value.strip("/").replace("Date(", "").replace(")", ""))
    moment = datetime.datetime(1970, 1, 1) + datetime.timedelta(
        milliseconds=milliseconds)
    return moment.date().isoformat()


class PostingDateCase(MockServerCase):
    def post(self, baseline="", posting="2026-09-11", document="2026-09-11",
             receivable=None):
        body = {
            "DOCUMENTHEADER": {"COMP_CODE": "1710", "DOC_TYPE": "KR",
                               "DOC_DATE": document, "PSTNG_DATE": posting},
            "ACCOUNTPAYABLE": [{"ITEMNO_ACC": "1", "VENDOR_NO": "1000001",
                                "PMNTTRMS": "NT30", "BLINE_DATE": baseline}],
            "ACCOUNTGL": [{"ITEMNO_ACC": "2", "GL_ACCOUNT": "0000400000"}],
            "CURRENCYAMOUNT": [
                {"ITEMNO_ACC": "1", "CURRENCY": "EUR", "AMT_DOCCUR": "-100.00"},
                {"ITEMNO_ACC": "2", "CURRENCY": "EUR", "AMT_DOCCUR": "100.00"}],
        }
        if receivable is not None:
            body["ACCOUNTRECEIVABLE"] = [{"ITEMNO_ACC": "3", "CUSTOMER": "1000001",
                                          "BLINE_DATE": receivable}]
            body["CURRENCYAMOUNT"].append(
                {"ITEMNO_ACC": "3", "CURRENCY": "EUR", "AMT_DOCCUR": "0.00"})
        status, _, out = self.request(
            "POST", "/sap/bc/rfc/BAPI_ACC_DOCUMENT_POST", body=body,
            headers=self.csrf_token())
        return status, out

    def posted(self, **kw):
        status, out = self.post(**kw)
        self.assertEqual(status, 200, out)
        self.assertEqual(out["RETURN"][0]["TYPE"], "S", out["RETURN"])
        return out["OBJ_KEY"][:10]

    def payable(self, document):
        _, _, body = self.get(
            CUBE + "?$filter=AccountingDocument%%20eq%%20'%s'%%20and%%20"
            "AccountingDocumentItemType%%20eq%%20'K'&$format=json" % document)
        [line] = body["d"]["results"]
        return line

    def written(self):
        counts = []
        for entity_set in ("A_JournalEntry", "A_JournalEntryItem"):
            status, _, raw = self.get(JOURNAL + "/%s/$count" % entity_set, raw=True)
            self.assertEqual(status, 200)
            counts.append(int(raw))
        return tuple(counts)


class TestSapsOwnDateFormat(PostingDateCase):
    def test_yyyymmdd_is_a_baseline_date(self):
        """30 September and thirty days: 30 October, on every interpreter."""
        line = self.payable(self.posted(baseline="20260930"))
        self.assertEqual(on_the_wire(line["NetDueDate"]), "2026-10-30")

    def test_and_means_what_the_dashed_form_means(self):
        line = self.payable(self.posted(baseline="2026-09-30"))
        self.assertEqual(on_the_wire(line["NetDueDate"]), "2026-10-30")

    def test_yyyymmdd_is_a_posting_date_and_a_document_date(self):
        document = self.posted(posting="20260911", document="20260910")
        _, _, body = self.get(
            JOURNAL + "/A_JournalEntry?$filter=AccountingDocument%%20eq%%20"
            "'%s'&$format=json" % document)
        [entry] = body["d"]["results"]
        self.assertEqual(on_the_wire(entry["PostingDate"]), "2026-09-11")
        self.assertEqual(on_the_wire(entry["DocumentDate"]), "2026-09-10")
        self.assertEqual(entry["FiscalYear"], "2026")
        # no baseline of its own, so the payable falls due from the posting date
        self.assertEqual(on_the_wire(self.payable(document)["NetDueDate"]),
                         "2026-10-11")

    def test_an_initial_date_is_a_date_left_blank(self):
        """ABAP sends 00000000 for a date field nobody filled in."""
        line = self.payable(self.posted(baseline="00000000"))
        self.assertEqual(on_the_wire(line["NetDueDate"]), "2026-10-11")


class TestADateNobodyCanRead(PostingDateCase):
    def refused(self, **kw):
        before = self.written()
        status, out = self.post(**kw)

        self.assertEqual(status, 200, "a business refusal, not a transport one")
        self.assertEqual(out["OBJ_KEY"], "")
        self.assertEqual({row["TYPE"] for row in out["RETURN"]}, {"E"})
        self.assertEqual(self.written(), before,
                         "no header, and no line under it")
        return out["RETURN"]

    def test_a_baseline_date_that_is_not_one_is_a_return_row(self):
        [row] = self.refused(baseline="next Tuesday")

        self.assertEqual(row["PARAMETER"], "ACCOUNTPAYABLE")
        self.assertEqual(row["ROW"], 1)
        self.assertEqual(row["FIELD"], "BLINE_DATE")
        self.assertIn("next Tuesday", row["MESSAGE"])

    def test_eight_digits_are_not_enough_to_be_a_date(self):
        [row] = self.refused(baseline="20261345")
        self.assertEqual(row["FIELD"], "BLINE_DATE")

    def test_neither_are_the_right_dashes(self):
        [row] = self.refused(baseline="2026-02-30")
        self.assertEqual(row["FIELD"], "BLINE_DATE")

    def test_a_posting_date_is_held_to_the_same(self):
        [row] = self.refused(posting="11.09.2026")

        self.assertEqual(row["PARAMETER"], "DOCUMENTHEADER")
        self.assertEqual(row["FIELD"], "PSTNG_DATE")

    def test_every_unreadable_date_is_named_at_once(self):
        rows = self.refused(document="yesterday", baseline="soon",
                            receivable="later")

        self.assertEqual(
            sorted((row["PARAMETER"], row["ROW"], row["FIELD"]) for row in rows),
            [("ACCOUNTPAYABLE", 1, "BLINE_DATE"),
             ("ACCOUNTRECEIVABLE", 1, "BLINE_DATE"),
             ("DOCUMENTHEADER", 0, "DOC_DATE")])

    def test_the_document_after_a_refusal_posts(self):
        self.refused(baseline="next Tuesday")
        before = self.written()
        self.posted(baseline="20260930")
        self.assertEqual(self.written(), (before[0] + 1, before[1] + 2))


class TestPostingItselfWritesNothingItCannotFinish(PostingDateCase):
    """Underneath the BAPI, for whichever caller forgets to check first.

    `post_journal_entry` is called in process here because every route to it
    over HTTP now refuses the date before it gets this far - which is the
    fix, and also why the guard below it has no other way to be seen.
    """

    def test_a_bad_baseline_on_the_second_line_leaves_no_first_line(self):
        ctx = Context(self.httpd.mock.conn, self.base)
        before = self.written()

        with self.assertRaises(ValueError):
            documents.post_journal_entry(ctx, {"PostingDate": "2026-09-11"}, [
                {"GLAccount": "0000400000", "Amount": "100.00"},
                {"Supplier": "1000001", "Amount": "-100.00",
                 "DueCalculationBaseDate": "next Tuesday"}])

        self.assertEqual(self.written(), before)

    def test_as_date_reads_both_shapes_and_nothing_else(self):
        self.assertEqual(documents.as_date("20260930"), datetime.date(2026, 9, 30))
        self.assertEqual(documents.as_date("2026-09-30"), datetime.date(2026, 9, 30))
        self.assertEqual(documents.as_date("2026-09-30T00:00:00"),
                         datetime.date(2026, 9, 30))
        # the one place the answer used to be the interpreter's
        self.assertEqual(documents.net_due_date("20260930", "NT30"), "2026-10-30")
        for not_a_date in ("", "2026-9-30", "30.09.2026", "202609301", "2026093",
                           "2026-02-30", "20260230", "2026-09-30 and then some"):
            with self.assertRaises(ValueError, msg=not_a_date):
                documents.as_date(not_a_date)


class TestAnInvoicDatedOnNoDay(PostingDateCase):
    """The same date, arriving by IDoc: status 51, and nothing written."""

    def test_it_is_not_posted_and_leaves_no_document(self):
        idoc = (
            '<?xml version="1.0"?><INVOIC02><IDOC BEGIN="1">'
            '<EDI_DC40 SEGMENT="1"><IDOCTYP>INVOIC02</IDOCTYP>'
            "<MESTYP>INVOIC</MESTYP></EDI_DC40>"
            '<E1EDK01 SEGMENT="1"><CURCY>EUR</CURCY><ZTERM>NT30</ZTERM></E1EDK01>'
            '<E1EDKA1 SEGMENT="1"><PARVW>LF</PARVW><PARTN>1000009</PARTN></E1EDKA1>'
            '<E1EDK02 SEGMENT="1"><QUALF>009</QUALF><BELNR>NO-SUCH-DAY</BELNR></E1EDK02>'
            '<E1EDK03 SEGMENT="1"><IDDAT>026</IDDAT><DATUM>20261345</DATUM></E1EDK03>'
            '<E1EDS01 SEGMENT="1"><SUMID>010</SUMID><SUMME>119.00</SUMME></E1EDS01>'
            "</IDOC></INVOIC02>")
        before = self.written()
        status, _, receipt = self.request(
            "POST", "/sap/bc/idoc", body=idoc,
            headers=dict(self.csrf_token(), **{"Content-Type": "application/xml",
                                               "Accept": "application/json"}))

        self.assertEqual(status, 201, receipt)
        self.assertEqual(receipt["STATUS"], "51", receipt)
        self.assertIn("20261345", receipt["STATUS_TEXT"])
        self.assertEqual(self.written(), before)


if __name__ == "__main__":
    unittest.main()
