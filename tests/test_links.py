"""$links: the addressable form of an association, read and written."""
import unittest

from support import MockServerCase, SRV

ITEM = SRV + "/A_SalesOrderItem(SalesOrder='%s',SalesOrderItem='%s')"


class TestLinks(MockServerCase):
    def orders(self, count=2):
        _, _, body = self.get(SRV + "/A_SalesOrder?$top=%d&$format=json" % count)
        return [row["SalesOrder"] for row in body["d"]["results"]]

    # -- reading -----------------------------------------------------------
    def test_to_many_links_are_uris(self):
        order = self.orders(1)[0]
        status, headers, body = self.get(
            SRV + "/A_SalesOrder('%s')/$links/to_Item?$format=json" % order)
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("DataServiceVersion"), "2.0")
        results = body["d"]["results"]
        self.assertTrue(results)
        for link in results:
            self.assertEqual(list(link), ["uri"], "a link carries a uri and nothing else")
            self.assertIn("A_SalesOrderItem(SalesOrder='%s'" % order, link["uri"])

        # the URIs must address the entities they claim to
        status, _, entity = self.get(
            results[0]["uri"].split(self.base, 1)[1] + "?$format=json")
        self.assertEqual(status, 200)
        self.assertEqual(entity["d"]["SalesOrder"], order)

    def test_to_one_link_is_a_single_uri(self):
        order = self.orders(1)[0]
        status, _, body = self.get(
            (ITEM % (order, "000010")) + "/$links/to_SalesOrder?$format=json")
        self.assertEqual(status, 200)
        self.assertEqual(list(body["d"]), ["uri"])
        self.assertTrue(body["d"]["uri"].endswith("A_SalesOrder('%s')" % order))

    def test_paging_and_counting(self):
        order = self.orders(1)[0]
        _, _, full = self.get(SRV + "/A_SalesOrder('%s')/$links/to_Item?$format=json" % order)
        total = len(full["d"]["results"])

        _, _, page = self.get(
            SRV + "/A_SalesOrder('%s')/$links/to_Item"
                  "?$top=1&$inlinecount=allpages&$format=json" % order)
        self.assertEqual(len(page["d"]["results"]), 1)
        self.assertEqual(int(page["d"]["__count"]), total)

        _, _, skipped = self.get(
            SRV + "/A_SalesOrder('%s')/$links/to_Item?$skip=1&$format=json" % order)
        self.assertEqual(len(skipped["d"]["results"]), total - 1)

        _, _, count = self.get(
            SRV + "/A_SalesOrder('%s')/$links/to_Item/$count" % order, raw=True)
        self.assertEqual(int(count), total)

    def test_unknown_navigation_and_stray_segments(self):
        order = self.orders(1)[0]
        for path in ("/$links/to_Nothing", "/$links", "/$links/to_Item/nonsense"):
            status, _, _ = self.get(
                SRV + "/A_SalesOrder('%s')%s?$format=json" % (order, path))
            self.assertIn(status, (400, 404), path)

    # -- writing -----------------------------------------------------------
    def test_setting_a_link_to_its_current_target_succeeds(self):
        order = self.orders(1)[0]
        headers = self.csrf_token()
        status, _, _ = self.request(
            "PUT", (ITEM % (order, "000010")) + "/$links/to_SalesOrder",
            body={"uri": "%s%s/A_SalesOrder('%s')" % (self.base, SRV, order)},
            headers=headers)
        self.assertEqual(status, 204)

    def test_repointing_a_composition_is_rejected(self):
        first, second = self.orders(2)
        headers = self.csrf_token()

        # an item cannot be moved to another order: its order is part of its key
        status, _, body = self.request(
            "PUT", (ITEM % (first, "000010")) + "/$links/to_SalesOrder",
            body={"uri": "%s%s/A_SalesOrder('%s')" % (self.base, SRV, second)},
            headers=headers)
        self.assertEqual(status, 400)
        self.assertIn("SalesOrder", body["error"]["message"]["value"])

        # and the same seen from the other end
        status, _, body = self.request(
            "POST", SRV + "/A_SalesOrder('%s')/$links/to_Item" % first,
            body={"uri": "%s%s" % (self.base, ITEM % (second, "000010"))},
            headers=headers)
        self.assertEqual(status, 400)

        # the item stayed where it was
        _, _, entity = self.get((ITEM % (first, "000010")) + "?$format=json")
        self.assertEqual(entity["d"]["SalesOrder"], first)

    def test_deleting_a_composition_link_is_rejected(self):
        order = self.orders(1)[0]
        headers = self.csrf_token()
        status, _, body = self.request(
            "DELETE",
            SRV + "/A_SalesOrder('%s')/$links/to_Item(SalesOrder='%s',SalesOrderItem='000010')"
            % (order, order), headers=headers)
        self.assertEqual(status, 400)
        self.assertIn("cannot be changed", body["error"]["message"]["value"])

        status, _, _ = self.get((ITEM % (order, "000010")) + "?$format=json")
        self.assertEqual(status, 200, "the item must still exist")

    def test_bad_link_payloads(self):
        first, second = self.orders(2)
        headers = self.csrf_token()
        cases = [
            ("POST", SRV + "/A_SalesOrder('%s')/$links/to_Item" % first, {}, 400),
            ("POST", SRV + "/A_SalesOrder('%s')/$links/to_Item" % first,
             {"uri": "%s%s/A_SalesOrder('%s')" % (self.base, SRV, second)}, 400),
            ("POST", SRV + "/A_SalesOrder('%s')/$links/to_Item" % first,
             {"uri": "%s%s/A_SalesOrderItem(SalesOrder='0000000000',"
                     "SalesOrderItem='000010')" % (self.base, SRV)}, 404),
            ("PUT", SRV + "/A_SalesOrder('%s')/$links/to_Item" % first,
             {"uri": "whatever"}, 405),
            ("POST", (ITEM % (first, "000010")) + "/$links/to_SalesOrder",
             {"uri": "whatever"}, 405),
            ("DELETE", SRV + "/A_SalesOrder('%s')/$links/to_Item" % first, None, 400),
        ]
        for method, path, payload, expected in cases:
            status, _, body = self.request(method, path, body=payload, headers=headers)
            self.assertEqual(status, expected, "%s %s" % (method, path))
            self.assertIn("error", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
