"""OData V4: the same documents as V2, in the shapes of the other dialect."""
import json
import unittest
from xml.etree import ElementTree as ET

from support import MockServerCase, SRV

V4 = "/sap/opu/odata4/sap/api_salesorder/srvd_a2x/sap/api_salesorder/0001"
BP4 = "/sap/opu/odata4/sap/api_businesspartner/srvd_a2x/sap/api_businesspartner/0001"
EDM_NS = "{http://docs.oasis-open.org/odata/ns/edm}"


class V4Case(MockServerCase):
    def an_order(self):
        _, _, body = self.get(V4 + "/SalesOrder?$top=1")
        return body["value"][0]


class TestV4Shapes(V4Case):
    def test_collection_envelope(self):
        status, headers, body = self.get(V4 + "/SalesOrder?$top=2&$count=true")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("OData-Version"), "4.0")
        self.assertTrue(body["@odata.context"].endswith("$metadata#SalesOrder"))
        self.assertIsInstance(body["@odata.count"], int)
        self.assertEqual(len(body["value"]), 2)
        self.assertNotIn("d", body)

    def test_value_formats_differ_from_v2(self):
        order = self.an_order()
        # numbers are numbers, timestamps are ISO 8601 with a zone
        self.assertIsInstance(order["TotalNetAmount"], float)
        self.assertRegex(order["CreationDate"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertNotIn("__metadata", order)
        self.assertIn("@odata.etag", order)
        self.assertNotIn("to_Item", order, "V4 omits links to what was not expanded")

        # the very same row through the V2 service
        _, _, v2 = self.get(
            SRV + "/A_SalesOrder('%s')?$format=json" % order["SalesOrder"])
        entity = v2["d"]
        self.assertIsInstance(entity["TotalNetAmount"], str)
        self.assertRegex(entity["CreationDate"], r"^/Date\(-?\d+\)/$")
        self.assertEqual(float(entity["TotalNetAmount"]), order["TotalNetAmount"])
        self.assertEqual(entity["__metadata"]["etag"], order["@odata.etag"])

    def test_single_entity_and_context(self):
        order = self.an_order()
        status, headers, body = self.get(V4 + "/SalesOrder('%s')" % order["SalesOrder"])
        self.assertEqual(status, 200)
        self.assertTrue(body["@odata.context"].endswith("$metadata#SalesOrder/$entity"))
        self.assertEqual(body["SalesOrder"], order["SalesOrder"])
        self.assertEqual(headers.get("ETag"), order["@odata.etag"])

    def test_select_and_expand(self):
        _, _, body = self.get(V4 + "/SalesOrder?$top=1&$select=SalesOrder,SoldToParty")
        row = body["value"][0]
        self.assertEqual(set(row) - {"@odata.etag"}, {"SalesOrder", "SoldToParty"})
        self.assertIn("#SalesOrder(SalesOrder,SoldToParty)", body["@odata.context"])

        _, _, body = self.get(V4 + "/SalesOrder?$top=1&$expand=to_Item")
        row = body["value"][0]
        self.assertIsInstance(row["to_Item"], list, "V4 expands to a bare array")
        self.assertTrue(row["to_Item"])

    def test_nested_expand_options(self):
        _, _, body = self.get(
            V4 + "/SalesOrder?$top=1&$select=SalesOrder"
                 "&$expand=to_Item($select=SalesOrderItem;$top=1;"
                 "$orderby=SalesOrderItem desc;$count=true)")
        row = body["value"][0]
        self.assertEqual(len(row["to_Item"]), 1)
        self.assertEqual(set(row["to_Item"][0]), {"SalesOrderItem"})
        self.assertGreaterEqual(row["to_Item@odata.count"], 1)
        self.assertGreaterEqual(row["to_Item@odata.count"], len(row["to_Item"]))

    def test_error_shape(self):
        status, _, body = self.get(V4 + "/SalesOrder?$filter=Nope%20eq%201")
        self.assertEqual(status, 400)
        self.assertIsInstance(body["error"]["message"], str,
                              "V4 messages are plain strings, not {lang, value}")
        self.assertTrue(body["error"]["code"].startswith("/IWBEP/"))
        self.assertTrue(body["error"]["details"])

    def test_dialects_do_not_mix(self):
        status, _, body = self.get(V4 + "/SalesOrder?$inlinecount=allpages")
        self.assertEqual(status, 400)
        self.assertIn("V2", body["error"]["message"])

        status, _, body = self.get(SRV + "/A_SalesOrder?$count=true&$format=json")
        self.assertEqual(status, 400)
        self.assertIn("V4", body["error"]["message"]["value"])

        status, _, body = self.get(
            SRV + "/A_SalesOrder?$expand=to_Item($select=Material)&$format=json")
        self.assertEqual(status, 400)
        self.assertIn("V4", body["error"]["message"]["value"])

    def test_count_segment_and_ref(self):
        order = self.an_order()
        _, _, count = self.get(V4 + "/SalesOrder/$count", raw=True)
        self.assertGreater(int(count), 0)

        status, _, body = self.get(V4 + "/SalesOrder('%s')/to_Item/$ref" % order["SalesOrder"])
        self.assertEqual(status, 200)
        self.assertTrue(body["value"])
        self.assertIn("@odata.id", body["value"][0])
        self.assertIn("/SalesOrderItem(", body["value"][0]["@odata.id"])

    def test_service_document(self):
        _, _, body = self.get(V4 + "/")
        self.assertTrue(body["@odata.context"].endswith("$metadata"))
        names = {entry["name"] for entry in body["value"]}
        self.assertEqual(names, {
            "SalesOrder", "SalesOrderItem", "SalesOrderHeaderPartner",
            "SalesOrderPartnerAddress", "SalesOrderText",
            "SalesOrderHeaderPrElement", "SalesOrderItemPrElement",
        })
        self.assertEqual(body["value"][0]["kind"], "EntitySet")


class TestV4Metadata(V4Case):
    def test_csdl_xml(self):
        status, headers, raw = self.get(V4 + "/$metadata", raw=True)
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("OData-Version"), "4.0")
        root = ET.fromstring(raw)
        self.assertEqual(root.get("Version"), "4.0")
        text = raw.decode()
        self.assertIn('<EntityType Name="SalesOrderType">', text)
        self.assertIn('<EntitySet Name="SalesOrder"', text)
        self.assertIn("Collection(com.sap.gateway.srvd_a2x.api_salesorder.v0001."
                      "SalesOrderItemType)", text)
        self.assertIn("<NavigationPropertyBinding", text)
        self.assertNotIn("<Association", text, "V4 has no Association elements")
        self.assertIn('Type="Edm.DateTimeOffset"', text)

    def test_csdl_json(self):
        status, _, body = self.get(V4 + "/$metadata?$format=json")
        self.assertEqual(status, 200)
        self.assertEqual(body["$Version"], "4.0")
        schema = body["com.sap.gateway.srvd_a2x.api_salesorder.v0001"]
        self.assertEqual(schema["SalesOrderType"]["$Kind"], "EntityType")
        self.assertEqual(schema["SalesOrderType"]["$Key"], ["SalesOrder"])
        self.assertTrue(body["$EntityContainer"].endswith("api_salesorder_Container"))

    def test_v2_metadata_is_untouched(self):
        _, _, raw = self.get(SRV + "/$metadata", raw=True)
        text = raw.decode()
        self.assertIn('<edmx:Edmx Version="1.0"', text)
        self.assertIn("<Association ", text)
        self.assertIn('Type="Edm.DateTime"', text)


class TestV4Writes(V4Case):
    def payload(self):
        return {
            "SalesOrderType": "OR", "SalesOrganization": "1710",
            "DistributionChannel": "10", "OrganizationDivision": "00",
            "SoldToParty": "1000001", "TransactionCurrency": "EUR",
            "to_Item": [{"Material": "TG11", "RequestedQuantity": "3",
                         "NetAmount": "300.00"}],
        }

    def test_create_patch_delete(self):
        headers = self.csrf_token()
        status, resp_headers, created = self.request(
            "POST", V4 + "/SalesOrder?$expand=to_Item", body=self.payload(), headers=headers)
        self.assertEqual(status, 201)
        self.assertEqual(resp_headers.get("OData-Version"), "4.0")
        self.assertIn("Location", resp_headers)
        self.assertTrue(created["@odata.context"].endswith("$metadata#SalesOrder/$entity"))
        order = created["SalesOrder"]
        self.assertIsInstance(created["to_Item"], list)
        self.assertEqual(created["TotalNetAmount"], 300.0)

        status, _, _ = self.request(
            "PATCH", V4 + "/SalesOrder('%s')" % order,
            body={"PurchaseOrderByCustomer": "PO-V4"},
            headers=dict(headers, **{"If-Match": created["@odata.etag"]}))
        self.assertEqual(status, 204)

        # and the change is visible through the V2 service, same row underneath
        _, _, v2 = self.get(SRV + "/A_SalesOrder('%s')?$format=json" % order)
        self.assertEqual(v2["d"]["PurchaseOrderByCustomer"], "PO-V4")

        status, _, _ = self.request("DELETE", V4 + "/SalesOrder('%s')" % order,
                                    headers=dict(headers, **{"If-Match": "*"}))
        self.assertEqual(status, 204)
        status, _, _ = self.get(V4 + "/SalesOrder('%s')" % order)
        self.assertEqual(status, 404)

    def test_json_batch_with_atomicity_group(self):
        headers = self.csrf_token()
        batch = {"requests": [
            {"id": "1", "method": "GET", "url": "SalesOrder?$top=1"},
            {"id": "2", "method": "POST", "url": "SalesOrder", "atomicityGroup": "g1",
             "headers": {"content-type": "application/json"},
             "body": self.payload()},
            {"id": "3", "method": "POST", "url": "SalesOrder", "atomicityGroup": "g1",
             "headers": {"content-type": "application/json"},
             "body": {"ThisIsNotAProperty": "x"}},
        ]}
        _, _, before = self.get(V4 + "/SalesOrder/$count", raw=True)
        status, _, body = self.request("POST", V4 + "/$batch", body=batch,
                                       headers=dict(headers, **{
                                           "Content-Type": "application/json"}))
        self.assertEqual(status, 200)
        statuses = {r["id"]: r["status"] for r in body["responses"]}
        self.assertEqual(statuses["1"], 200)
        self.assertEqual(statuses["3"], 400)
        self.assertNotIn("2", statuses, "the whole atomicity group is rolled back")

        _, _, after = self.get(V4 + "/SalesOrder/$count", raw=True)
        self.assertEqual(int(before), int(after))


class TestV4BusinessPartner(V4Case):
    def test_second_v4_service(self):
        status, _, body = self.get(BP4 + "/BusinessPartner?$top=1&$expand=to_BusinessPartnerAddress")
        self.assertEqual(status, 200)
        partner = body["value"][0]
        self.assertIsInstance(partner["to_BusinessPartnerAddress"], list)
        self.assertIsInstance(partner["BusinessPartnerIsBlocked"], bool)

    def test_v4_services_are_listed_with_their_version(self):
        _, _, services = self.get("/_mock/services")
        v4 = {s["name"]: s for s in services["services"] if s["odataVersion"] == 4}
        self.assertTrue({"api_salesorder", "api_businesspartner"} <= set(v4), v4)
        for service in v4.values():
            self.assertIn("/sap/opu/odata4/", service["url"])
            self.assertTrue(service["entitySets"])


class TestEveryV4Service(V4Case):
    """The V4 services are declarations over the same types the V2 ones serve."""

    SERVICES = {
        "api_salesorder": ("SalesOrder", "/sap/opu/odata/sap/API_SALES_ORDER_SRV",
                           "A_SalesOrder"),
        "api_businesspartner": ("BusinessPartner",
                                "/sap/opu/odata/sap/API_BUSINESS_PARTNER_SRV",
                                "A_BusinessPartner"),
        "api_product": ("Product", "/sap/opu/odata/sap/API_PRODUCT_SRV", "A_Product"),
        "api_purchaseorder": ("PurchaseOrder",
                              "/sap/opu/odata/sap/API_PURCHASEORDER_PROCESS_SRV",
                              "A_PurchaseOrder"),
    }

    def path_of(self, name):
        return "/sap/opu/odata4/sap/%s/srvd_a2x/sap/%s/0001" % (name, name)

    def test_each_service_answers_in_v4(self):
        for name, (entity_set, _v2_path, _type) in self.SERVICES.items():
            base = self.path_of(name)
            status, headers, body = self.get("%s/%s?$top=1&$count=true" % (base, entity_set))
            self.assertEqual(status, 200, name)
            self.assertEqual(headers.get("OData-Version"), "4.0", name)
            self.assertIn("@odata.context", body, name)
            self.assertGreater(body["@odata.count"], 0, name)

    def test_each_service_has_valid_csdl(self):
        for name in self.SERVICES:
            status, _, raw = self.get(self.path_of(name) + "/$metadata", raw=True)
            self.assertEqual(status, 200, name)
            root = ET.fromstring(raw)
            self.assertEqual(root.get("Version"), "4.0", name)
            self.assertNotIn("<Association", raw.decode(), name)

    def test_the_same_rows_underlie_both_dialects(self):
        for name, (entity_set, v2_path, v2_set) in self.SERVICES.items():
            _, _, v4 = self.get("%s/%s/$count" % (self.path_of(name), entity_set), raw=True)
            _, _, v2 = self.get("%s/%s/$count" % (v2_path, v2_set), raw=True)
            self.assertEqual(int(v4), int(v2), name)

    def test_expansion_stays_inside_the_dialect(self):
        base = self.path_of("api_product")
        status, _, body = self.get(base + "/Product?$top=1&$expand=to_Description,to_Plant")
        self.assertEqual(status, 200)
        product = body["value"][0]
        self.assertIsInstance(product["to_Description"], list)
        self.assertNotIn("__metadata", product["to_Description"][0],
                         "an expanded entity must not fall back to V2 shapes")

        # the same navigation through the V2 service keeps its own shape
        _, _, v2 = self.get("/sap/opu/odata/sap/API_PRODUCT_SRV/A_Product"
                            "?$top=1&$expand=to_Description&$format=json")
        self.assertIn("results", v2["d"]["results"][0]["to_Description"])

    def test_purchase_order_items_expand(self):
        base = self.path_of("api_purchaseorder")
        _, _, body = self.get(base + "/PurchaseOrder?$top=1&$expand=to_PurchaseOrderItem")
        order = body["value"][0]
        self.assertTrue(order["to_PurchaseOrderItem"])
        item = order["to_PurchaseOrderItem"][0]
        self.assertEqual(item["PurchaseOrder"], order["PurchaseOrder"])
        self.assertIsInstance(item["OrderQuantity"], float)
        self.assertIsInstance(item["IsCompletelyDelivered"], bool)

    def test_writes_reach_the_same_rows(self):
        base = self.path_of("api_product")
        headers = self.csrf_token()
        status, _, _ = self.request(
            "PATCH", base + "/Product('TG11')",
            body={"ProductGroup": "L099"}, headers=dict(headers, **{"If-Match": "*"}))
        self.assertEqual(status, 204)
        _, _, v2 = self.get(
            "/sap/opu/odata/sap/API_PRODUCT_SRV/A_Product('TG11')?$format=json")
        self.assertEqual(v2["d"]["ProductGroup"], "L099")


if __name__ == "__main__":
    unittest.main(verbosity=2)
