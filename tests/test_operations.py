"""Operating the mock: failure scenarios, fault rules, the client check and /_mock."""
from __future__ import annotations

import unittest

from support import BP_SRV, MockServerCase, SRV


class TestOperations(MockServerCase):
    def test_scenario_header(self):
        status, headers, body = self.get(
            SRV + "/A_SalesOrder?$format=json", headers={"sap-mock-scenario": "busy"})
        self.assertEqual(status, 503)
        self.assertEqual(headers.get("Retry-After"), "30")
        self.assertIn("work process", body["error"]["message"]["value"])

        status, _, _ = self.get(SRV + "/A_SalesOrder?sap-mock-scenario=error&$format=json")
        self.assertEqual(status, 500)

    def test_fault_rules(self):
        status, _, rule = self.request(
            "POST", "/_mock/faults",
            body={"match": "A_BusinessPartner", "method": "GET", "status": 500,
                  "message": "Injected", "count": 1})
        self.assertEqual(status, 201)
        status, _, body = self.get(BP_SRV + "/A_BusinessPartner?$format=json")
        self.assertEqual(status, 500)
        self.assertEqual(body["error"]["message"]["value"], "Injected")
        status, _, _ = self.get(BP_SRV + "/A_BusinessPartner?$format=json")
        self.assertEqual(status, 200, "rule with count=1 must fire only once")
        self.request("DELETE", "/_mock/faults")

    def test_wrong_client_is_rejected(self):
        status, _, body = self.get(SRV + "/A_SalesOrder?sap-client=200&$format=json")
        self.assertEqual(status, 400)
        self.assertIn("200", body["error"]["message"]["value"])

    def test_mock_admin_endpoints(self):
        status, _, health = self.get("/_mock/health")
        self.assertEqual(health["status"], "UP")
        status, _, state = self.get("/_mock/state")
        self.assertGreater(state["A_SalesOrder"], 0)
        status, _, requests = self.get("/_mock/requests?limit=5")
        self.assertTrue(requests["results"])
        self.assertIn("path", requests["results"][0])
        status, _, services = self.get("/_mock/services")
        self.assertEqual(len(services["services"]), 4)

    def test_reset_restores_seed_data(self):
        headers = self.csrf_token()
        _, _, before = self.get(SRV + "/A_SalesOrder/$count", raw=True)
        self.request("POST", SRV + "/A_SalesOrder", headers=headers, body={
            "SalesOrderType": "OR", "SalesOrganization": "1710", "SoldToParty": "1000001",
            "DistributionChannel": "10", "OrganizationDivision": "00"})
        _, _, grown = self.get(SRV + "/A_SalesOrder/$count", raw=True)
        self.assertEqual(int(grown), int(before) + 1)
        status, _, body = self.request("POST", "/_mock/reset", body={"orders": 7})
        self.assertEqual(status, 200)
        self.assertEqual(body["counts"]["A_SalesOrder"], 7)
        _, _, after = self.get(SRV + "/A_SalesOrder/$count", raw=True)
        self.assertEqual(int(after), 7)

    def test_index_page(self):
        status, headers, body = self.get("/", raw=True)
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn(b"Mock SAP system", body)

if __name__ == "__main__":
    unittest.main(verbosity=2)
