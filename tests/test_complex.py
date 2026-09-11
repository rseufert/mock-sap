"""Complex (structured) types, over the classic GWSAMPLE_BASIC demo service."""
import unittest
from xml.etree import ElementTree as ET

from support import MockServerCase

GW = "/sap/opu/odata/IWBEP/GWSAMPLE_BASIC"
EDM_NS = "{http://schemas.microsoft.com/ado/2008/09/edm}"


class ComplexCase(MockServerCase):
    def a_partner(self):
        _, _, body = self.get(GW + "/BusinessPartnerSet?$top=1&$format=json")
        return body["d"]["results"][0]


class TestComplexTypes(ComplexCase):
    def test_structured_property_is_nested_on_the_wire(self):
        partner = self.a_partner()
        address = partner["Address"]
        self.assertEqual(address["__metadata"]["type"], "GWSAMPLE_BASIC.CT_Address")
        self.assertEqual(
            set(address) - {"__metadata"},
            {"City", "PostalCode", "Street", "Building", "Country", "AddressType"})
        self.assertTrue(address["City"])
        self.assertNotIn("Address_City", partner, "the flattened columns must not leak")

    def test_filter_on_a_sub_property(self):
        _, _, body = self.get(
            GW + "/BusinessPartnerSet?$filter=Address/Country eq 'DE'&$format=json")
        results = body["d"]["results"]
        self.assertTrue(results)
        for row in results:
            self.assertEqual(row["Address"]["Country"], "DE")

        # and combined with an ordinary property
        _, _, body = self.get(
            GW + "/BusinessPartnerSet?$filter=Address/Country eq 'DE' and "
                 "startswith(CompanyName,'B')&$format=json")
        for row in body["d"]["results"]:
            self.assertTrue(row["CompanyName"].startswith("B"))

    def test_orderby_a_sub_property(self):
        _, _, body = self.get(
            GW + "/BusinessPartnerSet?$orderby=Address/City desc&$format=json")
        cities = [row["Address"]["City"] for row in body["d"]["results"]]
        self.assertEqual(cities, sorted(cities, reverse=True))

    def test_select_whole_structure_or_one_path(self):
        _, _, body = self.get(
            GW + "/BusinessPartnerSet?$top=1&$select=BusinessPartnerID,Address&$format=json")
        row = body["d"]["results"][0]
        self.assertEqual(set(row) - {"__metadata"}, {"BusinessPartnerID", "Address"})
        self.assertIn("PostalCode", row["Address"])

        _, _, body = self.get(
            GW + "/BusinessPartnerSet?$top=1&$select=Address/City&$format=json")
        row = body["d"]["results"][0]
        self.assertEqual(set(row) - {"__metadata"}, {"Address"})
        self.assertEqual(set(row["Address"]) - {"__metadata"}, {"City"})

    def test_bad_paths_are_rejected(self):
        for query in ("$filter=Address/Nope eq 'x'",
                      "$filter=Address eq 'x'",
                      "$orderby=Address/Nope",
                      "$select=Address/Nope"):
            status, _, body = self.get(GW + "/BusinessPartnerSet?%s&$format=json" % query)
            self.assertEqual(status, 400, query)
            self.assertIn("error", body)

    def test_write_and_partial_update(self):
        headers = self.csrf_token()
        payload = {
            "BusinessPartnerID": "0100009001", "CompanyName": "Newco Ltd",
            "CurrencyCode": "EUR", "BusinessPartnerRole": "01",
            "Address": {"City": "Lisbon", "PostalCode": "1100-148",
                        "Street": "Rua Augusta", "Building": "7",
                        "Country": "PT", "AddressType": "02"},
        }
        status, _, created = self.request(
            "POST", GW + "/BusinessPartnerSet", body=payload, headers=headers)
        self.assertEqual(status, 201)
        self.assertEqual(created["d"]["Address"]["City"], "Lisbon")
        self.assertEqual(created["d"]["Address"]["Country"], "PT")

        status, _, _ = self.request(
            "PATCH", GW + "/BusinessPartnerSet('0100009001')",
            body={"Address": {"City": "Porto"}},
            headers=dict(headers, **{"If-Match": "*"}))
        self.assertEqual(status, 204)

        _, _, entity = self.get(GW + "/BusinessPartnerSet('0100009001')?$format=json")
        address = entity["d"]["Address"]
        self.assertEqual(address["City"], "Porto")
        self.assertEqual(address["Street"], "Rua Augusta",
                         "a partial update must leave the rest of the structure alone")

        status, _, body = self.request(
            "PATCH", GW + "/BusinessPartnerSet('0100009001')",
            body={"Address": {"Nope": "x"}}, headers=dict(headers, **{"If-Match": "*"}))
        self.assertEqual(status, 400)
        self.assertIn("CT_Address", body["error"]["message"]["value"])

        status, _, body = self.request(
            "PATCH", GW + "/BusinessPartnerSet('0100009001')",
            body={"Address": "not a structure"}, headers=dict(headers, **{"If-Match": "*"}))
        self.assertEqual(status, 400)

    def test_metadata_declares_the_complex_type(self):
        status, _, raw = self.get(GW + "/$metadata", raw=True)
        self.assertEqual(status, 200)
        root = ET.fromstring(raw)
        complex_types = root.iter(EDM_NS + "ComplexType")
        names = {ct.get("Name") for ct in complex_types}
        self.assertIn("CT_Address", names)

        text = raw.decode()
        self.assertIn('<Property Name="Address" Type="GWSAMPLE_BASIC.CT_Address"', text)
        self.assertIn('<EntitySet Name="BusinessPartnerSet" '
                      'EntityType="GWSAMPLE_BASIC.BusinessPartner"', text)


class TestGwsampleService(ComplexCase):
    """The demo service itself: classic naming, and the usual surfaces on it."""

    def test_entity_sets_are_named_apart_from_their_types(self):
        _, _, body = self.get(GW + "/?$format=json")
        self.assertEqual(
            set(body["d"]["EntitySets"]),
            {"BusinessPartnerSet", "ContactSet", "ProductSet", "SalesOrderSet",
             "SalesOrderLineItemSet"})
        partner = self.a_partner()
        self.assertEqual(partner["__metadata"]["type"], "GWSAMPLE_BASIC.BusinessPartner")
        self.assertIn("/BusinessPartnerSet('", partner["__metadata"]["uri"])

    def test_it_is_listed_in_the_catalog_under_its_own_prefix(self):
        _, _, body = self.get(
            "/sap/opu/odata/IWFND/CATALOGSERVICE;v=2/ServiceCollection?$format=json")
        entry = [row for row in body["d"]["results"]
                 if row["TechnicalServiceName"] == "GWSAMPLE_BASIC"]
        self.assertEqual(len(entry), 1)
        self.assertTrue(entry[0]["ServiceUrl"].endswith("/sap/opu/odata/IWBEP/GWSAMPLE_BASIC"))

    def test_expand_across_the_demo_service(self):
        _, _, body = self.get(
            GW + "/SalesOrderSet?$top=1&$expand=ToLineItems,ToBusinessPartner&$format=json")
        order = body["d"]["results"][0]
        self.assertTrue(order["ToLineItems"]["results"])
        self.assertEqual(order["ToBusinessPartner"]["BusinessPartnerID"], order["CustomerID"])
        # an expanded entity carries its structured property too
        self.assertEqual(order["ToBusinessPartner"]["Address"]["__metadata"]["type"],
                         "GWSAMPLE_BASIC.CT_Address")

    def test_navigation_and_links_still_work_here(self):
        _, _, body = self.get(GW + "/SalesOrderSet?$top=1&$format=json")
        order = body["d"]["results"][0]["SalesOrderID"]
        status, _, items = self.get(
            GW + "/SalesOrderSet('%s')/ToLineItems?$format=json" % order)
        self.assertEqual(status, 200)
        self.assertTrue(items["d"]["results"])

        status, _, links = self.get(
            GW + "/SalesOrderSet('%s')/$links/ToLineItems?$format=json" % order)
        self.assertEqual(status, 200)
        self.assertEqual(len(links["d"]["results"]), len(items["d"]["results"]))
        self.assertIn("/SalesOrderLineItemSet(", links["d"]["results"][0]["uri"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
