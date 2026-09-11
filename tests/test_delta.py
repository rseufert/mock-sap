"""Delta handling: what changed since the last read, deletions included."""
import unittest

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

        _, _, body = self.get(self.relative(link))
        self.assertEqual([r["SalesOrder"] for r in body["value"]], [first])
        second_link = body["@odata.deltaLink"]
        self.assertNotEqual(second_link, link)

        second = self.create(V4 + "/SalesOrder")
        _, _, body = self.get(self.relative(second_link))
        self.assertEqual([r["SalesOrder"] for r in body["value"]], [second],
                         "the second read starts where the first one stopped")

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
