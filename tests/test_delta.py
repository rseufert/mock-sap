"""Delta handling: what changed since the last read, deletions included."""
import datetime
import time
import unittest

from mocksap import delta as delta_module
from support import MockServerCase, SRV

V4 = "/sap/opu/odata4/sap/api_salesorder/srvd_a2x/sap/api_salesorder/0001"
TRACK = {"Prefer": "odata.track-changes"}


class DeltaCase(MockServerCase):
    def order_payload(self, **extra):
        payload = {"SalesOrderType": "OR", "SalesOrganization": "1710",
                   "DistributionChannel": "10", "OrganizationDivision": "00",
                   "SoldToParty": "1000001", "TransactionCurrency": "EUR"}
        payload.update(extra)
        return payload

    def relative(self, link):
        return link.split(self.base, 1)[1]

    def create(self, base_path):
        status, _, body = self.request("POST", base_path, body=self.order_payload(),
                                       headers=self.csrf_token())
        self.assertEqual(status, 201)
        return body["d"]["SalesOrder"] if "d" in body else body["SalesOrder"]


class TestDeltaV4(DeltaCase):
    def first_read(self, query="?$top=1"):
        status, headers, body = self.get(V4 + "/SalesOrder" + query, headers=TRACK)
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Preference-Applied"), "odata.track-changes")
        return body["@odata.deltaLink"]

    def test_a_tracked_read_hands_back_a_link(self):
        link = self.first_read()
        self.assertIn("$deltatoken=", link)
        self.assertIn("/SalesOrder?", link)

        # nothing has happened yet
        status, _, body = self.get(self.relative(link))
        self.assertEqual(status, 200)
        self.assertEqual(body["value"], [])
        self.assertIn("#SalesOrder/$delta", body["@odata.context"])
        self.assertIn("$deltatoken=", body["@odata.deltaLink"])

    def test_changes_creations_and_deletions(self):
        _, _, body = self.get(V4 + "/SalesOrder?$top=3")
        existing = [row["SalesOrder"] for row in body["value"]]
        link = self.first_read()

        headers = self.csrf_token()
        self.request("PATCH", V4 + "/SalesOrder('%s')" % existing[0],
                     body={"PurchaseOrderByCustomer": "PO-DELTA"},
                     headers=dict(headers, **{"If-Match": "*"}))
        created = self.create(V4 + "/SalesOrder")
        self.request("DELETE", V4 + "/SalesOrder('%s')" % existing[1],
                     headers=dict(headers, **{"If-Match": "*"}))

        _, _, body = self.get(self.relative(link))
        changed = {row["SalesOrder"] for row in body["value"] if "@removed" not in row}
        removed = [row for row in body["value"] if "@removed" in row]

        self.assertIn(existing[0], changed, "the updated order")
        self.assertIn(created, changed, "the new order")
        self.assertNotIn(existing[2], changed, "an untouched order stays out")

        self.assertEqual(len(removed), 1)
        self.assertEqual(removed[0]["@removed"], {"reason": "deleted"})
        self.assertIn("SalesOrder('%s')" % existing[1], removed[0]["@id"])
        self.assertNotIn("SalesOrder", removed[0], "a removed entry carries no properties")

    def test_the_link_moves_on(self):
        link = self.first_read()
        headers = self.csrf_token()
        first = self.create(V4 + "/SalesOrder")

        # Step off the boundary deliberately. A delta read is at-least-once: a
        # change sharing the next token's millisecond is reported again rather
        # than risk being lost, so without this the assertion below is a race
        # against the clock - which is how it used to fail one run in seven.
        time.sleep(0.01)

        _, _, body = self.get(self.relative(link))
        self.assertEqual([r["SalesOrder"] for r in body["value"]], [first])
        second_link = body["@odata.deltaLink"]
        self.assertNotEqual(second_link, link)

        second = self.create(V4 + "/SalesOrder")
        _, _, body = self.get(self.relative(second_link))
        self.assertEqual([r["SalesOrder"] for r in body["value"]], [second],
                         "the second read starts where the first one stopped")
        self.assertNotIn(first, [r["SalesOrder"] for r in body["value"]],
                         "and does not repeat what the first one reported")

    def test_a_change_in_the_token_s_own_millisecond_is_not_lost(self):
        """The boundary, asked for exactly rather than raced for.

        Change timestamps carry milliseconds and nothing finer, so a change can
        share the token's instant. Comparing with `>` dropped it and never
        reported it again: the client was told nothing had changed. Here the
        token is minted at the entity's own change instant, which is that case
        every time instead of one run in seven.
        """
        order = self.create(V4 + "/SalesOrder")
        _, _, entity = self.get(V4 + "/SalesOrder('%s')" % order)
        changed_at = entity["LastChangeDate"]

        # V4 hands the timestamp back as ISO; mint a token at that same instant
        moment = datetime.datetime.strptime(
            changed_at[:26].rstrip("Z"),
            "%Y-%m-%dT%H:%M:%S.%f" if "." in changed_at else "%Y-%m-%dT%H:%M:%S")
        token = delta_module.mint(moment)

        status, _, body = self.get(V4 + "/SalesOrder?$deltatoken=" + token)
        self.assertEqual(status, 200)
        self.assertEqual([r["SalesOrder"] for r in body["value"]], [order],
                         "a change at the token's own instant must still be reported, "
                         "and nothing older than it should come with it")

    def test_the_token_is_taken_before_the_rows_are_read(self):
        """Otherwise a change between the query and the mint is never reported.

        The link a tracked read hands back cannot be later than the moment the
        read began, or the gap between them swallows changes silently.
        """
        before = datetime.datetime.utcnow()
        link = self.first_read()
        after = datetime.datetime.utcnow()

        token = link.split("$deltatoken=")[1].split("&")[0]
        minted = delta_module.read(token)
        self.assertLessEqual(before.replace(microsecond=0), minted)
        self.assertLessEqual(minted, after)

        # the rows that read returned are not re-reported, so the token is not
        # so early that every read repeats itself
        _, _, body = self.get(self.relative(link))
        self.assertEqual(body["value"], [])

    def test_a_filter_survives_in_the_link(self):
        status, _, body = self.get(
            V4 + "/SalesOrder?$filter=SalesOrganization eq '1710'".replace(" ", "%20"),
            headers=TRACK)
        self.assertEqual(status, 200)
        link = body["@odata.deltaLink"]
        self.assertIn("$filter=", link)

        self.create(V4 + "/SalesOrder")
        _, _, body = self.get(self.relative(link))
        self.assertTrue(body["value"])
        for row in body["value"]:
            if "@removed" not in row:
                self.assertEqual(row["SalesOrganization"], "1710")


class TestDeltaV2(DeltaCase):
    def test_the_v2_spelling(self):
        status, headers, body = self.get(
            SRV + "/A_SalesOrder?$top=1&$format=json", headers=TRACK)
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Preference-Applied"), "odata.track-changes")
        link = body["d"]["__delta"]
        self.assertIn("!deltatoken=", link)

        created = self.create(SRV + "/A_SalesOrder")
        _, _, body = self.get(self.relative(link))
        results = body["d"]["results"]
        self.assertIn(created, [r["SalesOrder"] for r in results])
        self.assertIn("__delta", body["d"])

        self.request("DELETE", SRV + "/A_SalesOrder('%s')" % created,
                     headers=dict(self.csrf_token(), **{"If-Match": "*"}))
        _, _, body = self.get(self.relative(link))
        deleted = [r for r in body["d"]["results"] if r["__metadata"].get("deleted")]
        self.assertEqual(len(deleted), 1)
        self.assertIn("A_SalesOrder('%s')" % created, deleted[0]["__metadata"]["uri"])


class TestDeltaLimits(DeltaCase):
    def test_a_type_without_a_change_timestamp_says_so(self):
        status, _, body = self.get(V4 + "/SalesOrderItem?$top=1", headers=TRACK)
        self.assertEqual(status, 400)
        self.assertIn("change timestamp", body["error"]["message"])

    def test_tokens_this_system_did_not_issue(self):
        for token in ("whatever", "D2026T00", "D20261301T000000000"):
            status, _, body = self.get(V4 + "/SalesOrder?$deltatoken=" + token)
            self.assertEqual(status, 400, token)
            self.assertIn("error", body)

    def test_without_the_preference_there_is_no_link(self):
        _, _, body = self.get(V4 + "/SalesOrder?$top=1")
        self.assertNotIn("@odata.deltaLink", body)
        _, _, body = self.get(SRV + "/A_SalesOrder?$top=1&$format=json")
        self.assertNotIn("__delta", body["d"])

    def test_deletions_are_forgotten_on_reset(self):
        link = self.first_link()
        created = self.create(V4 + "/SalesOrder")
        self.request("DELETE", V4 + "/SalesOrder('%s')" % created,
                     headers=dict(self.csrf_token(), **{"If-Match": "*"}))
        _, _, body = self.get(self.relative(link))
        self.assertTrue([r for r in body["value"] if "@removed" in r])

        self.request("POST", "/_mock/reset")
        _, _, body = self.get(self.relative(link))
        self.assertEqual([r for r in body["value"] if "@removed" in r], [],
                         "a reset forgets the deletions along with everything else")

    def first_link(self):
        _, _, body = self.get(V4 + "/SalesOrder?$top=1", headers=TRACK)
        return body["@odata.deltaLink"]


if __name__ == "__main__":
    unittest.main(verbosity=2)
