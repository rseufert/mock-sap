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
