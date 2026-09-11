"""IDoc round-trip: ORDERS05 generation, inbound XML and flat files, status."""
from __future__ import annotations

import unittest
from xml.etree import ElementTree as ET

from support import MockServerCase, SRV


class TestIdoc(MockServerCase):
    def test_generate_and_receive(self):
        headers = self.csrf_token()
        _, _, body = self.get(SRV + "/A_SalesOrder?$top=1&$format=json")
        order = body["d"]["results"][0]["SalesOrder"]

        status, _, generated = self.request(
            "POST", "/sap/bc/idoc/generate", body={"SalesOrder": order}, headers=headers)
        self.assertEqual(status, 201)
        xml = generated["xml"]
        root = ET.fromstring(xml)
        self.assertEqual(root.tag, "ORDERS05")
        self.assertTrue(root.findall(".//E1EDP01"))
        self.assertEqual(root.find(".//EDI_DC40/IDOCTYP").text, "ORDERS05")

        status, _, receipt = self.request(
            "POST", "/sap/bc/idoc", body=xml,
            headers=dict(headers, **{"Content-Type": "application/xml",
                                     "Accept": "application/json"}))
        self.assertEqual(status, 201)
        self.assertEqual(receipt["STATUS"], "53")
        self.assertEqual(receipt["MESTYP"], "ORDERS")
        docnum = receipt["DOCNUM"]
        self.assertEqual(len(docnum), 16)

        status, _, stored = self.get("/sap/bc/idoc/%s" % docnum)
        self.assertEqual(stored["idoctyp"], "ORDERS05")

        status, _, updated = self.request(
            "PUT", "/sap/bc/idoc/%s/status" % docnum,
            body={"status": "51"}, headers=headers)
        self.assertEqual(updated["status_text"], "Application document not posted")

    def test_flat_idoc(self):
        headers = self.csrf_token()
        control = ("EDI_DC40  " + "100" + "0" * 16 + "0756" + "53" + "2" + "2" + " " + " "
                   + "ORDERS05".ljust(30) + " " * 30 + "ORDERS".ljust(30))
        status, _, receipt = self.request(
            "POST", "/sap/bc/idoc", body=control + "\nE1EDK01   ...",
            headers=dict(headers, **{"Content-Type": "text/plain"}))
        self.assertEqual(status, 201)
        self.assertEqual(receipt["IDOCTYP"], "ORDERS05")
        self.assertEqual(receipt["MESTYP"], "ORDERS")

if __name__ == "__main__":
    unittest.main(verbosity=2)
