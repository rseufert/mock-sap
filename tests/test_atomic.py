"""All of it or none of it: a write that fails partway leaves nothing behind.

Two places a partial write used to stay committed (#92). A deep insert wrote
its header and then its children, each committing as it went, so a refused
third item left a header and two items for an order the client was told did
not exist. And a changeset rolled back when a member *answered* with an
error, but not when one raised something nobody had turned into an answer.

Everything here is counted before and after rather than read off a response:
the response to a failed write is an error either way, and what matters is
what the database holds once it has been sent.
"""
from __future__ import annotations

import sqlite3
import unittest

from support import MockServerCase, SRV

from mocksap import batch  # noqa: E402 - support puts the checkout on the path

V4 = "/sap/opu/odata4/sap/api_salesorder/srvd_a2x/sap/api_salesorder/0001"
ORDER = {"SalesOrderType": "OR", "SalesOrganization": "1710",
         "DistributionChannel": "10", "OrganizationDivision": "00",
         "SoldToParty": "1000001", "TransactionCurrency": "EUR"}
ITEM = {"Material": "TG11", "RequestedQuantity": "2", "NetAmount": "1000.00",
        "RequestedQuantityUnit": "PC"}


class AtomicCase(MockServerCase):
    def count(self, entity_set):
        status, _, raw = self.get(SRV + "/%s/$count" % entity_set, raw=True)
        self.assertEqual(status, 200)
        return int(raw)

    def held(self):
        return self.count("A_SalesOrder"), self.count("A_SalesOrderItem")


class TestADeepInsertThatFailsPartway(AtomicCase):
    def post(self, items):
        return self.request("POST", SRV + "/A_SalesOrder",
                            body=dict(ORDER, to_Item=items),
                            headers=self.csrf_token())

    def test_a_refused_child_takes_the_header_with_it(self):
        before = self.held()
        status, _, body = self.post([ITEM, ITEM, dict(ITEM, NoSuchProperty="x")])

        self.assertEqual(status, 400, body)
        self.assertEqual(self.held(), before,
                         "no header, and neither of the two items before it")

    def test_so_does_a_child_that_fails_in_a_way_nobody_answered_for(self):
        """An item that is not an object is not a refusal the mock words.

        Whatever status that earns - it is a 500 today - the header and the
        item before it must not outlive it.
        """
        before = self.held()
        status, _, body = self.post([ITEM, 5])

        self.assertGreaterEqual(status, 400, body)
        self.assertEqual(self.held(), before)

    def test_the_order_after_a_failure_is_whole(self):
        """The failure left nothing open: the next deep insert commits."""
        self.post([ITEM, dict(ITEM, NoSuchProperty="x")])
        before = self.held()
        status, _, body = self.post([ITEM, ITEM])

        self.assertEqual(status, 201, body)
        self.assertEqual(self.held(), (before[0] + 1, before[1] + 2))
        self.assertEqual(body["d"]["TotalNetAmount"], "2000.000")


class TestAChangesetMemberThatRaises(AtomicCase):
    """The fault is put in from here, because it is one nobody foresaw.

    A member that answers 400 is the case `test_batch.py` already has. This
    is the other one: `dispatch` raising, the way it does when SQLite or a
    handler fails underneath it. There is no request that reliably does that
    - one that did would be a bug to fix - so the second member's dispatch is
    replaced by one that writes half of something and raises.
    """

    def break_the_second_member(self, exc):
        real, calls = batch.dispatch, []

        def dispatch(ctx, *args, **kwargs):
            calls.append(args)
            if len(calls) == 2:
                # Half a write, left uncommitted, as a handler that died
                # between its statements would leave it.
                ctx.conn.execute(
                    'UPDATE "A_SalesOrder" SET "PurchaseOrderByCustomer" = ?',
                    ("HALF-WRITTEN",))
                raise exc
            return real(ctx, *args, **kwargs)

        batch.dispatch = dispatch
        self.addCleanup(setattr, batch, "dispatch", real)
        return calls

    def half_written(self):
        _, _, raw = self.get(
            SRV + "/A_SalesOrder/$count?$filter=PurchaseOrderByCustomer%20eq%20"
            "'HALF-WRITTEN'", raw=True)
        return int(raw)

    def changeset(self):
        member = ("--changeset_1\r\n"
                  "Content-Type: application/http\r\n"
                  "Content-Transfer-Encoding: binary\r\n\r\n"
                  "POST A_SalesOrder HTTP/1.1\r\n"
                  "Content-Type: application/json\r\n\r\n"
                  '{"SalesOrderType":"OR","SalesOrganization":"1710",'
                  '"SoldToParty":"1000001"}\r\n')
        body = ("--batch_test\r\n"
                "Content-Type: multipart/mixed; boundary=changeset_1\r\n\r\n"
                + member + member + "--changeset_1--\r\n--batch_test--\r\n")
        headers = self.csrf_token()
        headers["Content-Type"] = "multipart/mixed; boundary=batch_test"
        return self.request("POST", SRV + "/$batch", body=body, headers=headers,
                            raw=True)

    def test_the_members_before_it_are_rolled_back(self):
        before = self.held()
        calls = self.break_the_second_member(
            sqlite3.OperationalError("disk I/O error"))

        status, _, raw = self.changeset()

        self.assertEqual(len(calls), 2, "the first member ran, and was real")
        self.assertEqual(status, 500, raw)
        self.assertEqual(self.held(), before,
                         "the first member's order went with the second's fault")
        self.assertEqual(self.half_written(), 0)

    def test_whatever_it_raises(self):
        before = self.held()
        self.break_the_second_member(KeyError("SoldToParty"))

        status, _, _ = self.changeset()

        self.assertEqual(status, 500)
        self.assertEqual(self.held(), before)
        self.assertEqual(self.half_written(), 0)

    def test_an_atomicity_group_is_rolled_back_the_same_way(self):
        before = self.held()
        self.break_the_second_member(ValueError("not a date"))
        member = {"method": "POST", "url": "SalesOrder", "atomicityGroup": "g1",
                  "headers": {"content-type": "application/json"},
                  "body": {"SalesOrderType": "OR", "SalesOrganization": "1710",
                           "SoldToParty": "1000001"}}

        status, _, _ = self.request(
            "POST", V4 + "/$batch",
            body={"requests": [dict(member, id="1"), dict(member, id="2")]},
            headers=dict(self.csrf_token(),
                         **{"Content-Type": "application/json"}))

        self.assertEqual(status, 500)
        self.assertEqual(self.held(), before)
        self.assertEqual(self.half_written(), 0)

    def test_the_mock_still_works_afterwards(self):
        self.break_the_second_member(sqlite3.OperationalError("disk I/O error"))
        self.changeset()
        self.doCleanups()                    # the real dispatch, back early
        before = self.held()

        status, _, raw = self.changeset()

        self.assertEqual(status, 202, raw)
        self.assertEqual(self.held()[0], before[0] + 2)


if __name__ == "__main__":
    unittest.main()
