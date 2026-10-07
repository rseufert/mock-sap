"""Writing through OData: CSRF, deep insert, update, delete and validation."""
from __future__ import annotations

import unittest
from xml.etree import ElementTree as ET

from support import BP_SRV, MockServerCase, SRV

EDM = "{http://schemas.microsoft.com/ado/2008/09/edm}"


class TestWrite(MockServerCase):
    def test_csrf_is_enforced(self):
        status, headers, body = self.request(
            "POST", SRV + "/A_SalesOrder", body={"SalesOrderType": "OR"},
            headers={"Content-Type": "application/json"})
        self.assertEqual(status, 403)
        self.assertEqual(headers.get("x-csrf-token"), "Required")
        self.assertIn("CSRF", body["error"]["message"]["value"])

    def test_deep_insert_patch_delete(self):
        headers = self.csrf_token()
        payload = {
            "SalesOrderType": "OR", "SalesOrganization": "1710",
            "DistributionChannel": "10", "OrganizationDivision": "00",
            "SoldToParty": "1000001", "TransactionCurrency": "EUR",
            "to_Item": [
                {"Material": "TG11", "RequestedQuantity": "2", "NetAmount": "1000.00",
                 "RequestedQuantityUnit": "PC"},
                {"Material": "TG13", "RequestedQuantity": "5", "NetAmount": "250.50",
                 "RequestedQuantityUnit": "PC"},
            ],
        }
        status, resp_headers, body = self.request(
            "POST", SRV + "/A_SalesOrder?$expand=to_Item", body=payload, headers=headers)
        self.assertEqual(status, 201)
        self.assertIn("Location", resp_headers)
        order = body["d"]["SalesOrder"]
        self.assertEqual(body["d"]["TotalNetAmount"], "1250.500")
        items = body["d"]["to_Item"]["results"]
        self.assertEqual([i["SalesOrderItem"] for i in items], ["000010", "000020"])
        self.assertEqual(body["d"]["CreatedByUser"], "MOCKUSER")

        status, _, _ = self.request(
            "PATCH", SRV + "/A_SalesOrder('%s')" % order,
            body={"PurchaseOrderByCustomer": "PO-TEST-1"}, headers=headers)
        self.assertEqual(status, 204)
        _, _, entity = self.get(SRV + "/A_SalesOrder('%s')?$format=json" % order)
        self.assertEqual(entity["d"]["PurchaseOrderByCustomer"], "PO-TEST-1")

        status, _, _ = self.request("DELETE", SRV + "/A_SalesOrder('%s')" % order,
                                    headers=headers)
        self.assertEqual(status, 204)
        status, _, _ = self.get(SRV + "/A_SalesOrder('%s')?$format=json" % order)
        self.assertEqual(status, 404)

    def test_validation_errors(self):
        headers = self.csrf_token()
        status, _, body = self.request(
            "POST", SRV + "/A_SalesOrder", body={"NotAProperty": "x"}, headers=headers)
        self.assertEqual(status, 400)
        self.assertIn("NotAProperty", body["error"]["message"]["value"])

        status, _, body = self.request(
            "POST", BP_SRV + "/A_BusinessPartnerAddress",
            body={"BusinessPartner": "1000001"}, headers=headers)
        self.assertEqual(status, 400)
        self.assertIn("AddressID", body["error"]["message"]["value"])

    def test_a_read_only_property_cannot_be_written(self):
        """`$metadata` says `sap:creatable="false"`; the mock now means it (#97).

        It stored whatever arrived, so a POST could set `CreatedByUser` to
        anything - a constraint a client generating its model from
        `$metadata` was told about and would have discovered was fiction
        only against the real system.
        """
        headers = self.csrf_token()
        status, _, body = self.request(
            "POST", SRV + "/A_SalesOrder", headers=headers,
            body=dict(self.an_order_payload(), CreatedByUser="IMPOSTOR"))
        self.assertEqual(status, 400)
        message = body["error"]["message"]["value"]
        self.assertIn("CreatedByUser", message)
        self.assertIn("sap:creatable", message)

        # and the same property on a PATCH, which is the updatable facet
        _, _, existing = self.get(SRV + "/A_SalesOrder?$top=1&$format=json")
        order = existing["d"]["results"][0]
        status, _, body = self.request(
            "PATCH", SRV + "/A_SalesOrder('%s')" % order["SalesOrder"],
            body={"TotalNetAmount": "999999.000"},
            headers=dict(headers, **{"If-Match": "*"}))
        self.assertEqual(status, 400)
        self.assertIn("sap:updatable", body["error"]["message"]["value"])

        _, _, after = self.get(
            SRV + "/A_SalesOrder('%s')?$format=json" % order["SalesOrder"])
        self.assertEqual(after["d"]["TotalNetAmount"], order["TotalNetAmount"],
                         "the refused PATCH changed nothing")

    def test_every_property_metadata_calls_read_only_is_refused(self):
        """Read off `$metadata`, not from a list here, which is the point.

        The check and the advertisement come from one declaration; this
        asserts that by taking the properties from the document the client
        would have read.
        """
        status, _, raw = self.get(SRV + "/$metadata", raw=True)
        self.assertEqual(status, 200)
        root = ET.fromstring(raw)
        sap = "{http://www.sap.com/Protocols/SAPData}"
        entity = [e for e in root.iter(EDM + "EntityType")
                  if e.get("Name") == "A_SalesOrderType"][0]
        read_only = [p.get("Name") for p in entity.iter(EDM + "Property")
                     if p.get(sap + "creatable") == "false"
                     and p.get("Name") not in ("SalesOrder",)]
        self.assertTrue(read_only, "the type should advertise some read-only fields")

        headers = self.csrf_token()
        for name in read_only:
            status, _, body = self.request(
                "POST", SRV + "/A_SalesOrder", headers=headers,
                body=dict(self.an_order_payload(), **{name: "x"}))
            self.assertEqual(status, 400, "POST %s should be refused" % name)
            self.assertIn(name, body["error"]["message"]["value"])

    def test_a_read_only_property_in_a_deep_insert_child_is_refused(self):
        """And refused before anything is written, children included."""
        headers = self.csrf_token()
        _, _, before = self.get(SRV + "/A_SalesOrder/$count", raw=True)
        status, _, body = self.request(
            "POST", SRV + "/A_SalesOrder", headers=headers,
            body=dict(self.an_order_payload(), to_Partner=[{
                "PartnerFunction": "AG", "Customer": "1000001",
                "PartnerFunctionInternalCode": "0001"}]))
        self.assertEqual(status, 400)
        self.assertIn("PartnerFunctionInternalCode", body["error"]["message"]["value"])

        _, _, after = self.get(SRV + "/A_SalesOrder/$count", raw=True)
        self.assertEqual(after, before, "nothing was created")

    def test_a_server_assigned_key_may_still_be_supplied(self):
        """`creatable=False` on a key means the server will assign it, not
        that a client may not send one: SAP lets a caller number its own
        item, and the BAPI layer passes ITM_NUMBER straight through."""
        headers = self.csrf_token()
        status, _, body = self.request(
            "POST", SRV + "/A_SalesOrder?$expand=to_Item", headers=headers,
            body=dict(self.an_order_payload(), to_Item=[{
                "SalesOrderItem": "000070", "Material": "TG11",
                "RequestedQuantity": "1", "RequestedQuantityUnit": "PC"}]))
        self.assertEqual(status, 201)
        self.assertEqual(body["d"]["to_Item"]["results"][0]["SalesOrderItem"],
                         "000070")

    def an_order_payload(self):
        return {"SalesOrderType": "OR", "SalesOrganization": "1710",
                "SoldToParty": "1000001", "DistributionChannel": "10",
                "OrganizationDivision": "00", "TransactionCurrency": "EUR"}

    def test_post_to_navigation(self):
        headers = self.csrf_token()
        _, _, body = self.get(SRV + "/A_SalesOrder?$top=1&$format=json")
        key = body["d"]["results"][0]["SalesOrder"]
        status, _, created = self.request(
            "POST", SRV + "/A_SalesOrder('%s')/to_Item" % key,
            body={"Material": "TG22", "RequestedQuantity": "1", "NetAmount": "99.00"},
            headers=headers)
        self.assertEqual(status, 201)
        self.assertEqual(created["d"]["SalesOrder"], key)

if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestSalesOrderWriteShapes(MockServerCase):
    """The shapes an order-management client posts: partner addresses, texts
    and pricing elements alongside the items."""

    def _order(self):
        return {
            "SalesOrderType": "OR", "SalesOrganization": "1710",
            "DistributionChannel": "10", "OrganizationDivision": "00",
            "SoldToParty": "1000001", "TransactionCurrency": "USD",
            "PurchaseOrderByCustomer": "#1042",
            "CustomerPurchaseOrderDate": "/Date(1788220800000)/",
            "SDDocumentReason": "100",
            "to_Partner": [{
                "PartnerFunction": "SH", "Customer": "1000001",
                "to_Address": [{
                    "OrganizationName1": "Acme GmbH",
                    "StreetName": "12 Maple St", "StreetSuffixName1": "Apt 4B",
                    "CityName": "Portland", "PostalCode": "97201",
                    "Region": "OR", "Country": "US", "CorrespondenceLanguage": "EN",
                }],
            }],
            "to_Text": [{"Language": "EN", "LongTextID": "0001", "LongText": "Leave at the gate."}],
            "to_Item": [{
                "SalesOrderItem": "10", "Material": "TG11",
                "RequestedQuantity": "2", "RequestedQuantityUnit": "PC",
                "PurchaseOrderByCustomer": "#1042",
                "to_PricingElement": [{
                    "PricingProcedureStep": "010", "PricingProcedureCounter": "01",
                    "ConditionType": "PPR0", "ConditionRateValue": "314.10",
                    "ConditionCurrency": "USD",
                }],
            }],
        }

    def test_deep_insert_carries_address_text_and_pricing(self):
        headers = self.csrf_token()
        status, _, body = self.request(
            "POST", SRV + "/A_SalesOrder", body=self._order(), headers=headers)
        self.assertEqual(status, 201)
        order = body["d"]["SalesOrder"]
        self.assertEqual(body["d"]["SDDocumentReason"], "100")
        self.assertTrue(body["d"]["CustomerPurchaseOrderDate"].startswith("/Date("))

        # Each child is readable through its own navigation, not just echoed back.
        _, _, partners = self.get(SRV + "/A_SalesOrder('%s')/to_Partner" % order)
        self.assertEqual([p["PartnerFunction"] for p in partners["d"]["results"]], ["SH"])

        _, _, address = self.get(
            SRV + "/A_SalesOrderPartnerAddress(SalesOrder='%s',PartnerFunction='SH')" % order)
        self.assertEqual(address["d"]["OrganizationName1"], "Acme GmbH")
        self.assertEqual(address["d"]["StreetSuffixName1"], "Apt 4B")
        self.assertEqual(address["d"]["CorrespondenceLanguage"], "EN")

        _, _, texts = self.get(SRV + "/A_SalesOrder('%s')/to_Text" % order)
        self.assertEqual(texts["d"]["results"][0]["LongText"], "Leave at the gate.")

        _, _, prices = self.get(
            SRV + "/A_SalesOrderItem(SalesOrder='%s',SalesOrderItem='10')/to_PricingElement" % order)
        rates = [p["ConditionRateValue"] for p in prices["d"]["results"]]
        self.assertEqual(len(rates), 1)
        self.assertEqual(float(rates[0]), 314.10)

    def test_server_assigned_child_keys_are_not_asked_for(self):
        # A client sends a condition type and a rate; SAP hands out the
        # procedure step and counter, as it hands out item numbers.
        headers = self.csrf_token()
        payload = self._order()
        payload["to_Item"][0]["to_PricingElement"] = [
            {"ConditionType": "PPR0", "ConditionRateValue": "314.10", "ConditionCurrency": "USD"},
            {"ConditionType": "RB00", "ConditionRateValue": "-14.10", "ConditionCurrency": "USD"},
        ]
        del payload["to_Item"][0]["SalesOrderItem"]
        status, _, body = self.request(
            "POST", SRV + "/A_SalesOrder", body=payload, headers=headers)
        self.assertEqual(status, 201)
        order = body["d"]["SalesOrder"]

        _, _, items = self.get(SRV + "/A_SalesOrder('%s')/to_Item" % order)
        item = items["d"]["results"][0]["SalesOrderItem"]
        self.assertEqual(item, "000010")

        _, _, prices = self.get(
            SRV + "/A_SalesOrderItem(SalesOrder='%s',SalesOrderItem='%s')/to_PricingElement"
            % (order, item))
        rows = prices["d"]["results"]
        self.assertEqual([r["ConditionType"] for r in rows], ["PPR0", "RB00"])
        # Numbered, and numbered apart.
        steps = [r["PricingProcedureStep"] for r in rows]
        self.assertEqual(len(set(steps)), 2)
        self.assertTrue(all(s.isdigit() for s in steps))

    def test_a_key_the_client_owns_is_still_required(self):
        headers = self.csrf_token()
        payload = self._order()
        del payload["to_Text"][0]["LongTextID"]
        status, _, body = self.request(
            "POST", SRV + "/A_SalesOrder", body=payload, headers=headers)
        self.assertEqual(status, 400)
        self.assertIn("LongTextID", body["error"]["message"]["value"])

    def test_a_misspelled_address_property_is_refused(self):
        # The point of carrying these shapes: a client that sends the business
        # partner address's spelling into a sales order hears about it here
        # rather than from a real gateway.
        headers = self.csrf_token()
        payload = self._order()
        payload["to_Partner"][0]["to_Address"][0] = {
            "BusinessPartnerName1": "Acme GmbH", "CityName": "Portland"}
        status, _, body = self.request(
            "POST", SRV + "/A_SalesOrder", body=payload, headers=headers)
        self.assertEqual(status, 400)
        self.assertIn("BusinessPartnerName1", body["error"]["message"]["value"])
