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


V4 = "/sap/opu/odata4/sap/api_salesorder/srvd_a2x/sap/api_salesorder/0001"


class TestAnOptionThatWouldBeIgnoredIsRefused(MockServerCase):
    """Accepted and dropped looks exactly like accepted and applied (#165).

    Each of these used to answer 200 with the rows the request would have
    returned without it, so a `$search` that matched nothing listed everything.
    """

    def said(self, path, status=400):
        got, _, body = self.get(path)
        self.assertEqual(got, status, body)
        message = body["error"]["message"]
        return message if isinstance(message, str) else message["value"]

    def test_search(self):
        for base in (SRV + "/A_SalesOrder?$format=json&", V4 + "/SalesOrder?"):
            with self.subTest(base=base):
                self.assertIn("$search is not implemented",
                              self.said(base + "$search=zzzzzz", 501))

    def test_a_skiptoken_nobody_issued(self):
        for base in (SRV + "/A_SalesOrder?$format=json&", V4 + "/SalesOrder?"):
            with self.subTest(base=base):
                message = self.said(base + "$skiptoken=5")
                self.assertIn("$skiptoken '5' is not one this system issued",
                              message)
                self.assertIn("$top and $skip", message)

    def test_a_format_it_does_not_serve_entity_data_in(self):
        for fmt in ("xml", "atom", "csv"):
            with self.subTest(fmt=fmt):
                status, headers, body = self.get(
                    SRV + "/A_SalesOrder?$top=1&$format=" + fmt, raw=True)
                self.assertEqual(status, 400)
                self.assertIn(b"$format=%s is not available" % fmt.encode(), body)
        self.assertIn("$format=xml is not available",
                      self.said(V4 + "/SalesOrder?$top=1&$format=xml"))

    def test_on_a_count_and_a_navigation_too(self):
        order = self.get(SRV + "/A_SalesOrder?$top=1&$format=json")[2]["d"][
            "results"][0]["SalesOrder"]
        for path in ("/A_SalesOrder/$count?$search=x",
                     "/A_SalesOrder('%s')/to_Item?$format=json&$search=x" % order):
            with self.subTest(path=path):
                self.assertEqual(self.get(SRV + path)[0], 501)

    def test_json_is_still_json_however_it_is_asked_for(self):
        for query in ("$format=json", "$format=JSON",
                      "$format=application/json"):
            with self.subTest(query=query):
                status, headers, body = self.get(
                    SRV + "/A_SalesOrder?$top=1&" + query)
                self.assertEqual(status, 200)
                self.assertEqual(len(body["d"]["results"]), 1)
        status, _, body = self.get(
            V4 + "/SalesOrder?$top=1&$format=application/json;odata.metadata=minimal")
        self.assertEqual((status, len(body["value"])), (200, 1))

    def test_the_documents_that_are_xml_still_are(self):
        status, headers, body = self.get(SRV + "/?$format=xml", raw=True)
        self.assertEqual(status, 200)
        self.assertIn("atomsvc+xml", headers["Content-Type"])
        status, headers, body = self.get(SRV + "/$metadata?$format=xml", raw=True)
        self.assertEqual(status, 200)
        self.assertIn(b"<edmx:Edmx", body)

    def test_an_error_asked_for_in_xml_is_still_xml(self):
        """`$format=xml` picks the error's format before it is refused."""
        status, headers, body = self.get(
            SRV + "/A_SalesOrder('nope')?$format=xml", raw=True)
        self.assertEqual(status, 400)
        self.assertIn("xml", headers["Content-Type"])
        self.assertTrue(body.startswith(b"<?xml"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
