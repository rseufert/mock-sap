"""System time: pinning it, moving it, and what refuses to move.

The point of a clock here is that a run is repeatable. Without one, every
date the mock computes comes from the wall clock, so the same script produces
different documents on different days and an example whose assertions depend
on a date goes stale by itself overnight - which is what happened to
mock-bank's `procure_to_pay` on a Sunday due date.
"""
from __future__ import annotations

import datetime
import re
import time
import unittest

from support import MockServerCase, SRV

from mocksap import clock

ISO_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


class TestTheClockOnItsOwn(unittest.TestCase):
    """The module, with no server in the way."""

    def test_unpinned_is_real_time(self):
        c = clock.Clock()
        self.assertAlmostEqual(
            (c.now() - datetime.datetime.utcnow()).total_seconds(), 0, delta=5)
        self.assertEqual(c.snapshot()["pinned"], "")

    def test_pinned_starts_where_it_was_told(self):
        c = clock.Clock("2026-10-02T16:00")
        self.assertEqual(c.today(), datetime.date(2026, 10, 2))
        self.assertEqual(c.now().hour, 16)
        self.assertEqual(c.now().tzinfo, None, "the contract is naive UTC")

    def test_it_keeps_ticking_rather_than_freezing(self):
        """An offset, not an instant - see the module docstring.

        A frozen clock stamps every row in a run identically, which in a log
        is indistinguishable from a bug.

        Waits for the value to change rather than asserting that some read
        has a non-zero microsecond. The first version did the latter and
        failed on Windows, whose clock resolution is about 15.6ms: both reads
        inside `Clock.__init__` and `now()` landed in the same tick, so the
        offset was exactly zero and `now()` returned the pinned moment on the
        nose. That assertion was measuring the host's clock resolution, not
        whether this clock advances.
        """
        c = clock.Clock("2026-10-02T16:00")
        first = c.now()
        deadline = time.monotonic() + 5.0
        while c.now() == first and time.monotonic() < deadline:
            pass
        later = c.now()
        self.assertGreater(later, first,
                           "a pinned clock that never moves is frozen")
        self.assertEqual(later.date(), datetime.date(2026, 10, 2),
                         "it ticked away from the pin instead of from real time")

    def test_the_shapes_it_accepts(self):
        for text, day in (("2026-10-02T16:00:30", datetime.date(2026, 10, 2)),
                          ("2026-10-02T16:00", datetime.date(2026, 10, 2)),
                          ("2026-10-02", datetime.date(2026, 10, 2))):
            self.assertEqual(clock.Clock(text).today(), day, text)

    def test_stamp_is_one_shape(self):
        c = clock.Clock("2026-10-02T16:00")
        self.assertTrue(ISO_Z.match(c.stamp()), c.stamp())
        self.assertEqual(c.stamp(), "2026-10-02T16:00:00Z")

    def test_advance_by_days_and_to_a_date(self):
        c = clock.Clock("2026-10-02T16:00")
        answer = c.advance(days=30)
        self.assertEqual(answer["calendarDays"], 30)
        self.assertEqual(c.today(), datetime.date(2026, 11, 1))

        c.advance(to="2026-12-25")
        self.assertEqual(c.today(), datetime.date(2026, 12, 25))

    def test_to_a_date_already_reached_is_a_move_of_no_distance(self):
        """Not a refusal: a test advancing to a due date that is today is fine."""
        c = clock.Clock("2026-10-02T16:00")
        answer = c.advance(to="2026-10-02")
        self.assertEqual(answer["calendarDays"], 0)
        self.assertEqual(c.today(), datetime.date(2026, 10, 2))
        self.assertEqual(c.now().hour, 16, "it did not fall back to midnight")

    def test_what_it_refuses_and_why(self):
        c = clock.Clock("2026-10-02T16:00")
        for kwargs, expected in (
                ({}, "either days=N or to="),
                ({"days": 1, "to": "2026-11-01"}, "either days=N or to="),
                ({"days": -1}, "whole number from 0"),
                ({"days": 4000}, "whole number from 0"),
                ({"days": 0.5}, "refused rather than rounded"),
                ({"days": float("nan")}, "not a number at all"),
                ({"days": float("inf")}, "not a number at all"),
                ({"days": True}, "not True"),
                ({"to": "2026-01-01"}, "does not go backwards"),
                ({"to": "not-a-date"}, "is not a date"),
                ({"to": "9999-12-31"}, "days ahead"),
        ):
            with self.assertRaises(clock.Invalid, msg=str(kwargs)) as caught:
                c.advance(**kwargs)
            self.assertIn(expected, str(caught.exception), str(kwargs))

    def test_a_far_future_date_is_refused_rather_than_overflowing(self):
        """Left unbounded it is accepted and the next advance raises OverflowError.

        That reaches a client as a 500 with a traceback instead of a refusal,
        and `days` already refuses the same range by name.
        """
        c = clock.Clock("2026-10-02T16:00")
        with self.assertRaises(clock.Invalid):
            c.advance(to="9999-12-31")
        self.assertEqual(c.today(), datetime.date(2026, 10, 2),
                         "a refused advance moved the clock anyway")

    def test_reset_returns_to_the_pin_not_to_real_time(self):
        c = clock.Clock("2026-10-02T16:00")
        c.advance(days=100)
        c.reset()
        self.assertEqual(c.today(), datetime.date(2026, 10, 2))

    def test_reset_of_an_unpinned_clock_returns_to_real_time(self):
        c = clock.Clock()
        c.advance(days=100)
        c.reset()
        self.assertAlmostEqual(
            (c.now() - datetime.datetime.utcnow()).total_seconds(), 0, delta=5)

    def test_a_bad_pin_is_refused_by_name(self):
        with self.assertRaises(clock.Invalid) as caught:
            clock.Clock("yesterday")
        self.assertIn("YYYY-MM-DDTHH:MM", str(caught.exception))


class TestTheClockOverHttp(MockServerCase):
    config_kwargs = {"clock": "2026-10-02T16:00", "csrf": False}

    def test_health_and_state_report_it(self):
        _, _, health = self.get("/_mock/health")
        self.assertEqual(health["clock"]["date"], "2026-10-02")
        self.assertEqual(health["clock"]["pinned"], "2026-10-02T16:00")

        _, _, state = self.get("/_mock/state")
        self.assertEqual(state["clock"]["date"], "2026-10-02")
        self.assertGreater(state["A_SalesOrder"], 0,
                           "the row counts are still the body")

    def test_advance_moves_it(self):
        status, _, answer = self.request("POST", "/_mock/advance?days=1")
        self.assertEqual(status, 200)
        self.assertEqual(answer["calendarDays"], 1)
        _, _, health = self.get("/_mock/health")
        self.assertEqual(health["clock"]["date"], "2026-10-03")
        self.request("POST", "/_mock/reset", body={})

    def test_advance_takes_a_body_as_well_as_a_query_string(self):
        status, _, answer = self.request("POST", "/_mock/advance", body={"days": 2})
        self.assertEqual(status, 200)
        self.assertEqual(answer["calendarDays"], 2)
        self.request("POST", "/_mock/reset", body={})

    def test_a_refusal_is_a_400_that_says_why(self):
        status, _, body = self.request("POST", "/_mock/advance?to=2020-01-01")
        self.assertEqual(status, 400)
        self.assertIn("does not go backwards", body["error"]["message"]["value"])

        status, _, body = self.request("POST", "/_mock/advance?days=0.5")
        self.assertEqual(status, 400)
        self.assertIn("rounded", body["error"]["message"]["value"])

    def test_reading_the_clock_is_not_moving_it(self):
        status, _, _ = self.get("/_mock/advance")
        self.assertEqual(status, 405)

    def test_reset_puts_the_clock_back_on_the_pin(self):
        self.request("POST", "/_mock/advance?days=50")
        _, _, answer = self.request("POST", "/_mock/reset", body={})
        self.assertEqual(answer["clock"]["date"], "2026-10-02")

    def test_a_written_entity_is_stamped_from_the_clock(self):
        """The whole point: the same script writes the same dates every run.

        Read through `CreationDate`, which the store sets on every insert.
        OData V2 carries a date as `/Date(milliseconds)/`, so the assertion
        decodes it rather than looking for an ISO string that is not there -
        which is what the first version of this test got wrong.
        """
        status, _, invoice = self.request(
            "POST", "/sap/opu/odata/sap/API_SUPPLIERINVOICE_PROCESS_SRV"
                    "/A_SupplierInvoice?$format=json",
            body={"CompanyCode": "1710", "InvoicingParty": "1000",
                  "SupplierInvoiceIDByInvcgParty": "CLOCK-1",
                  "InvoiceGrossAmount": "100.00", "DocumentCurrency": "EUR",
                  "PaymentTerms": "NT30"})
        self.assertIn(status, (200, 201), invoice)
        created = invoice["d"]["CreationDate"]
        self.assertTrue(created.startswith("/Date("), created)
        millis = int(created[len("/Date("):-len(")/")])
        stamped = (datetime.datetime(1970, 1, 1)
                   + datetime.timedelta(milliseconds=millis)).date()
        self.assertEqual(stamped, datetime.date(2026, 10, 2),
                         "the row was stamped from the wall clock, not the pin")

    def test_a_posted_invoice_dates_its_open_item_from_the_clock(self):
        """Posting is where the dates that matter are computed.

        The due date is the one that broke `procure_to_pay`: baseline plus the
        payment term's days, and with the clock pinned it is the same date on
        every run instead of thirty days from whenever the suite happened to
        run.
        """
        from mocksap import documents
        ctx = self.httpd.mock.context(self.base, "100")
        posted = documents.post_supplier_invoice(ctx, {
            "supplier": "1000", "reference": "CLOCK-2", "currency": "EUR",
            "terms": "NT30", "gross": "100.00", "net": "100.00",
            "tax": "0.00", "items": []})
        _, _, items = self.get("/_mock/open-items?supplier=1000")
        [item] = [i for i in items["results"]
                  if i["AccountingDocument"] == posted["accounting_document"]]
        self.assertTrue(item["PostingDate"].startswith("2026-10-02"),
                        item["PostingDate"])
        self.assertTrue(item["NetDueDate"].startswith("2026-11-01"),
                        "NT30 from the pinned day is 1 November, got %s"
                        % item["NetDueDate"])

    def test_the_logs_are_stamped_in_one_shape(self):
        self.get(SRV + "/A_SalesOrder?$top=1&$format=json")
        self.request("POST", "/sap/bc/rfc/BAPI_TRANSACTION_COMMIT", body={})
        for endpoint in ("/_mock/requests?limit=1", "/_mock/rfc-log?limit=1"):
            _, _, body = self.get(endpoint)
            stamp = body["results"][0]["ts"]
            self.assertTrue(ISO_Z.match(stamp), "%s gave %r" % (endpoint, stamp))
            self.assertTrue(stamp.startswith("2026-10-02"), stamp)


if __name__ == "__main__":
    unittest.main()
