"""Writing through OData: CSRF, deep insert, update, delete and validation."""
from __future__ import annotations

import unittest

from support import BP_SRV, MockServerCase, SRV


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
