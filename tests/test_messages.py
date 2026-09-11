"""sap-message: warnings that travel with a successful response."""
import json
import unittest
from xml.etree import ElementTree as ET

from support import BP_SRV, MockServerCase, SRV

V4 = "/sap/opu/odata4/sap/api_salesorder/srvd_a2x/sap/api_salesorder/0001"
PRODUCTS = "/sap/opu/odata/sap/API_PRODUCT_SRV"


class MessageCase(MockServerCase):
    def order_payload(self, **extra):
        payload = {
            "SalesOrderType": "OR", "SalesOrganization": "1710",
            "DistributionChannel": "10", "OrganizationDivision": "00",
            "SoldToParty": "1000001", "TransactionCurrency": "EUR",
        }
        payload.update(extra)
        return payload

    def messages_of(self, headers):
        raw = headers.get("sap-message")
        if raw is None:
            return []
        head = json.loads(raw)
        details = head.pop("details", [])
        return [head] + list(details)


class TestWarningsFromData(MessageCase):
    def test_a_delivery_date_in_the_past_is_moved_and_reported(self):
        headers = self.csrf_token()
        status, resp_headers, body = self.request(
            "POST", SRV + "/A_SalesOrder", headers=headers,
            body=self.order_payload(RequestedDeliveryDate="2020-01-01T00:00:00"))
        self.assertEqual(status, 201, "a warning must not fail the request")

        warnings = self.messages_of(resp_headers)
        self.assertEqual(len(warnings), 1)
        warning = warnings[0]
        self.assertEqual(warning["code"], "V1/302")
        self.assertEqual(warning["severity"], "warning")
        self.assertEqual(warning["numericSeverity"], 3)
        self.assertEqual(warning["target"], "RequestedDeliveryDate")
        self.assertIn("2020-01-01", warning["message"])

        # the document really was corrected, and the response shows the correction
        self.assertNotIn("/Date(1577", body["d"]["RequestedDeliveryDate"])
        _, _, entity = self.get(
            SRV + "/A_SalesOrder('%s')?$format=json" % body["d"]["SalesOrder"])
        self.assertEqual(entity["d"]["RequestedDeliveryDate"],
                         body["d"]["RequestedDeliveryDate"])

    def test_a_blocked_customer_is_reported(self):
        headers = self.csrf_token()
        status, _, _ = self.request(
            "PATCH", BP_SRV + "/A_BusinessPartner('1000002')",
            body={"BusinessPartnerIsBlocked": True},
            headers=dict(headers, **{"If-Match": "*"}))
        self.assertEqual(status, 204)

        status, resp_headers, _ = self.request(
            "POST", SRV + "/A_SalesOrder", headers=headers,
            body=self.order_payload(SoldToParty="1000002"))
        self.assertEqual(status, 201)
        warnings = self.messages_of(resp_headers)
        self.assertEqual(warnings[0]["code"], "F2/145")
        self.assertEqual(warnings[0]["target"], "SoldToParty")

    def test_a_material_flagged_for_deletion_is_reported(self):
        headers = self.csrf_token()
        self.request("PATCH", PRODUCTS + "/A_Product('TG13')",
                     body={"IsMarkedForDeletion": True},
                     headers=dict(headers, **{"If-Match": "*"}))

        status, resp_headers, _ = self.request(
            "POST", SRV + "/A_SalesOrder", headers=headers,
            body=self.order_payload(to_Item=[
                {"Material": "TG13", "RequestedQuantity": "1", "NetAmount": "10"}]))
        self.assertEqual(status, 201)
        warnings = self.messages_of(resp_headers)
        self.assertEqual(warnings[0]["code"], "M3/808")
        self.assertIn("TG13", warnings[0]["message"])

    def test_several_warnings_travel_together(self):
        headers = self.csrf_token()
        self.request("PATCH", PRODUCTS + "/A_Product('TG14')",
                     body={"IsMarkedForDeletion": True},
                     headers=dict(headers, **{"If-Match": "*"}))

        status, resp_headers, _ = self.request(
            "POST", SRV + "/A_SalesOrder", headers=headers,
            body=self.order_payload(
                RequestedDeliveryDate="2020-01-01T00:00:00",
                to_Item=[{"Material": "TG14", "RequestedQuantity": "1",
                          "NetAmount": "10"}]))
        self.assertEqual(status, 201)
        codes = [w["code"] for w in self.messages_of(resp_headers)]
        self.assertIn("V1/302", codes)
        self.assertIn("M3/808", codes)
        self.assertEqual(len(codes), 2, "the rest hang off the first message's details")

    def test_an_update_can_warn_too(self):
        headers = self.csrf_token()
        _, _, created = self.request("POST", SRV + "/A_SalesOrder",
                                     headers=headers, body=self.order_payload())
        order = created["d"]["SalesOrder"]
        status, resp_headers, _ = self.request(
            "PATCH", SRV + "/A_SalesOrder('%s')" % order,
            body={"RequestedDeliveryDate": "2019-05-05T00:00:00"},
            headers=dict(headers, **{"If-Match": "*"}))
        self.assertEqual(status, 204)
        self.assertEqual(self.messages_of(resp_headers)[0]["code"], "V1/302")

    def test_a_clean_request_carries_no_messages(self):
        headers = self.csrf_token()
        status, resp_headers, _ = self.request(
            "POST", SRV + "/A_SalesOrder", headers=headers,
            body=self.order_payload(RequestedDeliveryDate="2030-01-01T00:00:00"))
        self.assertEqual(status, 201)
        self.assertIsNone(resp_headers.get("sap-message"))

        status, resp_headers, _ = self.get(SRV + "/A_SalesOrder?$top=1&$format=json")
        self.assertIsNone(resp_headers.get("sap-message"))


class TestMessagesInV4(MessageCase):
    def test_the_entity_carries_them(self):
        headers = self.csrf_token()
        status, resp_headers, body = self.request(
            "POST", V4 + "/SalesOrder", headers=headers,
            body=self.order_payload(RequestedDeliveryDate="2020-01-01T00:00:00Z"))
        self.assertEqual(status, 201)
        self.assertIn("sap-message", resp_headers)
        self.assertEqual(len(body["SAP__Messages"]), 1)
        self.assertEqual(body["SAP__Messages"][0]["code"], "V1/302")
        self.assertEqual(body["SAP__Messages"][0]["numericSeverity"], 3)

    def test_metadata_declares_the_message_type(self):
        status, _, raw = self.get(V4 + "/$metadata", raw=True)
        self.assertEqual(status, 200)
        text = raw.decode()
        self.assertIn('<ComplexType Name="SAP__Message">', text)
        self.assertIn('<Property Name="SAP__Messages" Type="Collection(', text)
        ET.fromstring(raw)

        _, _, csdl = self.get(V4 + "/$metadata?$format=json")
        schema = csdl["com.sap.gateway.srvd_a2x.api_salesorder.v0001"]
        self.assertEqual(schema["SAP__Message"]["$Kind"], "ComplexType")
        self.assertTrue(schema["SalesOrderType"]["SAP__Messages"]["$Collection"])


class TestInjectedMessages(MessageCase):
    def tearDown(self):
        self.request("DELETE", "/_mock/faults")

    def test_a_rule_can_warn_without_failing(self):
        self.request("POST", "/_mock/faults", body={
            "match": "A_SalesOrder", "method": "GET",
            "message": "Credit limit exceeded for this customer", "count": 1})

        status, headers, body = self.get(SRV + "/A_SalesOrder?$top=1&$format=json")
        self.assertEqual(status, 200, "a message rule must not fail the request")
        warning = self.messages_of(headers)[0]
        self.assertEqual(warning["message"], "Credit limit exceeded for this customer")
        self.assertEqual(warning["severity"], "warning")
        self.assertTrue(body["d"]["results"])

        status, headers, _ = self.get(SRV + "/A_SalesOrder?$top=1&$format=json")
        self.assertIsNone(headers.get("sap-message"), "count=1 means once")

    def test_a_full_message_object_can_be_injected(self):
        self.request("POST", "/_mock/faults", body={
            "match": "A_SalesOrder", "method": "GET",
            "message": {"code": "V4/123", "message": "Pricing is provisional",
                        "severity": "info", "target": "TotalNetAmount",
                        "numericSeverity": 2, "transition": True}})
        status, headers, _ = self.get(SRV + "/A_SalesOrder?$top=1&$format=json")
        self.assertEqual(status, 200)
        warning = self.messages_of(headers)[0]
        self.assertEqual(warning["code"], "V4/123")
        self.assertEqual(warning["severity"], "info")
        self.assertEqual(warning["numericSeverity"], 2)

    def test_a_status_rule_still_fails(self):
        self.request("POST", "/_mock/faults", body={
            "match": "A_SalesOrder", "method": "GET", "status": 500,
            "message": "Backend unreachable"})
        status, headers, body = self.get(SRV + "/A_SalesOrder?$format=json")
        self.assertEqual(status, 500)
        self.assertIsNone(headers.get("sap-message"),
                          "a failure belongs in the error body, not a warning header")
        self.assertIn("error", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
