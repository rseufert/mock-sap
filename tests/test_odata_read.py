"""Reading through OData: envelopes, query options, navigation and errors."""
from __future__ import annotations

import unittest

from support import BP_SRV, MockServerCase, SRV


class TestRead(MockServerCase):
    def test_collection_envelope_and_shapes(self):
        status, headers, body = self.get(SRV + "/A_SalesOrder?$top=2&$format=json")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("DataServiceVersion"), "2.0")
        results = body["d"]["results"]
        self.assertEqual(len(results), 2)
        first = results[0]
        self.assertEqual(first["__metadata"]["type"], "API_SALES_ORDER_SRV.A_SalesOrderType")
        self.assertIn("A_SalesOrder('", first["__metadata"]["uri"])
        self.assertRegex(first["CreationDate"], r"^/Date\(-?\d+\)/$")
        self.assertRegex(first["TotalNetAmount"], r"^\d+\.\d{3}$")  # Edm.Decimal as string
        self.assertIn("__deferred", first["to_Item"])

    def test_filter_top_skip_orderby_inlinecount(self):
        _, _, body = self.get(
            SRV + "/A_SalesOrder?$filter=SalesOrganization eq '1710' and "
                  "TotalNetAmount gt 100&$orderby=TotalNetAmount desc&$top=3"
                  "&$inlinecount=allpages&$format=json")
        results = body["d"]["results"]
        self.assertLessEqual(len(results), 3)
        self.assertGreater(int(body["d"]["__count"]), len(results))
        amounts = [float(r["TotalNetAmount"]) for r in results]
        self.assertEqual(amounts, sorted(amounts, reverse=True))

    def test_filter_string_functions(self):
        _, _, body = self.get(
            BP_SRV + "/A_BusinessPartner?$filter=substringof('Becker',"
                     "BusinessPartnerFullName)&$format=json")
        names = [r["BusinessPartnerFullName"] for r in body["d"]["results"]]
        self.assertTrue(all("Becker" in n for n in names))
        self.assertTrue(names)

        _, _, body = self.get(
            BP_SRV + "/A_BusinessPartner?$filter=startswith(BusinessPartnerCategory,'1')"
                     " and endswith(SearchTerm1,'A')&$format=json")
        self.assertEqual(200, 200)

    def test_invalid_filter_returns_sap_error(self):
        status, _, body = self.get(SRV + "/A_SalesOrder?$filter=Nonsense eq 1&$format=json")
        self.assertEqual(status, 400)
        self.assertIn("error", body)
        self.assertIn("Nonsense", body["error"]["message"]["value"])
        self.assertTrue(body["error"]["code"].startswith("/IWBEP/"))

    def test_select_and_expand(self):
        _, _, body = self.get(
            SRV + "/A_SalesOrder?$top=1&$select=SalesOrder,SoldToParty&$format=json")
        row = body["d"]["results"][0]
        self.assertEqual(set(row) - {"__metadata"}, {"SalesOrder", "SoldToParty"})

        _, _, body = self.get(SRV + "/A_SalesOrder?$top=1&$expand=to_Item&$format=json")
        row = body["d"]["results"][0]
        self.assertIn("results", row["to_Item"])
        self.assertTrue(row["to_Item"]["results"])
        self.assertEqual(row["to_Item"]["results"][0]["__metadata"]["type"],
                         "API_SALES_ORDER_SRV.A_SalesOrderItemType")

    def test_entity_navigation_count_and_value(self):
        _, _, body = self.get(SRV + "/A_SalesOrder?$top=1&$format=json")
        key = body["d"]["results"][0]["SalesOrder"]

        status, _, entity = self.get(SRV + "/A_SalesOrder('%s')?$format=json" % key)
        self.assertEqual(status, 200)
        self.assertEqual(entity["d"]["SalesOrder"], key)

        status, _, items = self.get(SRV + "/A_SalesOrder('%s')/to_Item?$format=json" % key)
        self.assertEqual(status, 200)
        self.assertTrue(items["d"]["results"])

        status, _, count = self.get(SRV + "/A_SalesOrder('%s')/to_Item/$count" % key, raw=True)
        self.assertEqual(int(count), len(items["d"]["results"]))

        status, _, value = self.get(
            SRV + "/A_SalesOrder('%s')/SoldToParty/$value" % key, raw=True)
        self.assertEqual(value.decode(), entity["d"]["SoldToParty"])

        status, _, body = self.get(SRV + "/A_SalesOrder('0000000000')?$format=json")
        self.assertEqual(status, 404)

    def test_set_count(self):
        _, _, count = self.get(SRV + "/A_SalesOrder/$count", raw=True)
        _, _, body = self.get(SRV + "/A_SalesOrder?$inlinecount=allpages&$top=1&$format=json")
        self.assertEqual(int(count), int(body["d"]["__count"]))

if __name__ == "__main__":
    unittest.main(verbosity=2)
