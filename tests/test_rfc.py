"""BAPI/RFC over both transports: JSON and SOAP, success and failure."""
from __future__ import annotations

import unittest
from xml.etree import ElementTree as ET

from support import MockServerCase, SRV


class TestRfc(MockServerCase):
    def test_bapi_create_sales_order_json(self):
        headers = self.csrf_token()
        status, _, body = self.request(
            "POST", "/sap/bc/rfc/BAPI_SALESORDER_CREATEFROMDAT2", headers=headers,
            body={
                "ORDER_HEADER_IN": {"DOC_TYPE": "OR", "SALES_ORG": "1710",
                                    "DISTR_CHAN": "10", "DIVISION": "00", "CURRENCY": "EUR"},
                "ORDER_PARTNERS": [{"PARTN_ROLE": "AG", "PARTN_NUMB": "0001000001"}],
                "ORDER_ITEMS_IN": [{"ITM_NUMBER": "000010", "MATERIAL": "TG11",
                                    "REQ_QTY": "10", "COND_VALUE": "5000.00"}],
            })
        self.assertEqual(status, 200)
        self.assertRegex(body["SALESDOCUMENT"], r"^\d{10}$")
        first = body["RETURN"][0]
        self.assertEqual(first["TYPE"], "S")
        self.assertEqual(first["NUMBER"], "311")
        self.assertIn("SYSTEM", first)
        self.assertIn("LOG_MSG_NO", first)

        # the order is readable through OData afterwards
        _, _, entity = self.get(
            SRV + "/A_SalesOrder('%s')?$expand=to_Item&$format=json" % body["SALESDOCUMENT"])
        self.assertEqual(entity["d"]["TotalNetAmount"], "5000.000")

    def test_bapi_error_return_table(self):
        headers = self.csrf_token()
        _, _, body = self.request(
            "POST", "/sap/bc/rfc/BAPI_SALESORDER_CREATEFROMDAT2", headers=headers,
            body={"ORDER_HEADER_IN": {"DOC_TYPE": "OR"},
                  "ORDER_PARTNERS": [{"PARTN_ROLE": "AG", "PARTN_NUMB": "7777777"}]})
        self.assertEqual(body["RETURN"][0]["TYPE"], "E")
        self.assertIn("7777777", body["RETURN"][0]["MESSAGE"])

    def test_unknown_function_module(self):
        headers = self.csrf_token()
        status, _, body = self.request(
            "POST", "/sap/bc/rfc/BAPI_DOES_NOT_EXIST", headers=headers, body={})
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "RFC_ERROR_FUNCTION_NOT_FOUND")

    def test_soap_transport(self):
        headers = self.csrf_token()
        headers["Content-Type"] = "text/xml"
        envelope = (
            '<?xml version="1.0"?>'
            '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/">'
            "<soapenv:Body>"
            '<urn:MaterialGetDetail xmlns:urn="urn:sap-com:document:sap:soap:functions:mc-style">'
            "<Material>TG11</Material></urn:MaterialGetDetail>"
            "</soapenv:Body></soapenv:Envelope>")
        status, resp_headers, raw = self.request(
            "POST", "/sap/bc/srt/rfc/sap/materialgetdetail/100/materialgetdetail/binding",
            body=envelope, headers=headers, raw=True)
        self.assertEqual(status, 200)
        self.assertIn("xml", resp_headers["Content-Type"])
        root = ET.fromstring(raw)
        texts = [e.text for e in root.iter() if e.tag.endswith("MATL_DESC")]
        self.assertEqual(texts, ["Notebook Professional 15"])

    def test_soap_fault(self):
        headers = self.csrf_token()
        envelope = (
            '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/">'
            "<soapenv:Body><urn:NoSuchFunction "
            'xmlns:urn="urn:sap-com:document:sap:soap:functions:mc-style"/>'
            "</soapenv:Body></soapenv:Envelope>")
        status, _, raw = self.request("POST", "/sap/bc/srt/rfc/sap/x/100/x/x",
                                      body=envelope, headers=headers, raw=True)
        self.assertEqual(status, 500)
        self.assertIn("Fault", raw.decode())

if __name__ == "__main__":
    unittest.main(verbosity=2)
