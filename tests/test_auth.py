"""Authentication, on a server started with basic auth enabled."""
from __future__ import annotations

import unittest

from support import MockServerCase, SRV


class TestAuth(MockServerCase):
    config_kwargs = {"basic_auth": "sapuser:secret", "csrf": False}

    def test_basic_auth(self):
        status, headers, _ = self.get(SRV + "/A_SalesOrder?$format=json")
        self.assertEqual(status, 401)
        self.assertIn("SAP NetWeaver", headers["WWW-Authenticate"])

        import base64
        token = base64.b64encode(b"sapuser:secret").decode()
        status, _, body = self.get(SRV + "/A_SalesOrder?$top=1&$format=json",
                                   headers={"Authorization": "Basic " + token})
        self.assertEqual(status, 200)
        self.assertTrue(body["d"]["results"])

    def test_csrf_can_be_disabled(self):
        import base64
        token = base64.b64encode(b"sapuser:secret").decode()
        status, _, _ = self.request("POST", SRV + "/A_SalesOrder", body={
            "SalesOrderType": "OR", "SalesOrganization": "1710", "SoldToParty": "1000001",
            "DistributionChannel": "10", "OrganizationDivision": "00"},
            headers={"Authorization": "Basic " + token, "Content-Type": "application/json"})
        self.assertEqual(status, 201)

if __name__ == "__main__":
    unittest.main(verbosity=2)
