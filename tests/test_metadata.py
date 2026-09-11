"""Metadata surfaces: the service document, $metadata and the service catalog."""
from __future__ import annotations

import unittest
from xml.etree import ElementTree as ET

from support import MockServerCase, SRV


class TestMetadata(MockServerCase):
    def test_service_document_json(self):
        status, _, body = self.get(SRV + "/?$format=json")
        self.assertEqual(status, 200)
        self.assertIn("A_SalesOrder", body["d"]["EntitySets"])

    def test_metadata_is_edmx(self):
        status, headers, body = self.get(SRV + "/$metadata", raw=True)
        self.assertEqual(status, 200)
        self.assertIn("xml", headers["Content-Type"])
        root = ET.fromstring(body)
        self.assertTrue(root.tag.endswith("Edmx"))
        text = body.decode()
        self.assertIn('Name="A_SalesOrderType"', text)
        self.assertIn('sap:label="Sales Order"', text)
        self.assertIn('<EntitySet Name="A_SalesOrder"', text)
        self.assertIn("assoc_A_SalesOrder_to_Item", text)

    def test_catalog_service(self):
        status, _, body = self.get(
            "/sap/opu/odata/IWFND/CATALOGSERVICE;v=2/ServiceCollection?$format=json")
        self.assertEqual(status, 200)
        names = [r["TechnicalServiceName"] for r in body["d"]["results"]]
        self.assertIn("API_BUSINESS_PARTNER_SRV", names)

if __name__ == "__main__":
    unittest.main(verbosity=2)
