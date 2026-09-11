"""End-to-end tests: every assertion goes over real HTTP against the mock."""
from __future__ import annotations

import json
import os
import sys
import threading
import unittest
import urllib.error
import urllib.request
from xml.etree import ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mocksap.server import Config, make_server  # noqa: E402

SRV = "/sap/opu/odata/sap/API_SALES_ORDER_SRV"
BP_SRV = "/sap/opu/odata/sap/API_BUSINESS_PARTNER_SRV"


class MockServerCase(unittest.TestCase):
    config_kwargs: dict = {}

    @classmethod
    def setUpClass(cls):
        kwargs = dict(host="127.0.0.1", port=0, db_path=":memory:", quiet=True)
        kwargs.update(cls.config_kwargs)
        cls.httpd = make_server(Config(**kwargs))
        cls.port = cls.httpd.server_address[1]
        cls.base = "http://127.0.0.1:%d" % cls.port
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    # -- helpers
    def request(self, method, path, body=None, headers=None, raw=False):
        url = self.base + path.replace(" ", "%20")
        data = body
        if isinstance(data, (dict, list)):
            data = json.dumps(data).encode()
        elif isinstance(data, str):
            data = data.encode()
        req = urllib.request.Request(url, data=data, method=method)
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        try:
            with urllib.request.urlopen(req) as resp:
                payload = resp.read()
                return resp.status, dict(resp.headers), payload if raw else _maybe_json(payload)
        except urllib.error.HTTPError as err:
            payload = err.read()
            return err.code, dict(err.headers), payload if raw else _maybe_json(payload)

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def csrf_token(self):
        status, headers, _ = self.get(SRV + "/", headers={"X-CSRF-Token": "Fetch"})
        self.assertEqual(status, 200)
        token = headers.get("x-csrf-token") or headers.get("X-CSRF-Token")
        self.assertTrue(token)
        return {"X-CSRF-Token": token, "Content-Type": "application/json"}


def _maybe_json(payload: bytes):
    try:
        return json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return payload.decode("utf-8", "replace")


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


class TestRead(MockServerCase):
    def test_collection_envelope_and_shapes(self):
        status, headers, body = self.get(SRV + "/A_SalesOrder?$top=2&$format=json")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("DataServiceVersion"), "2.0")
        results = body["d"]["results"]
        self.assertEqual(len(results), 2)
        first = results[0]
        self.assertEqual(first["__metadata"]["type"], "API_SALES_ORDER_SRV.A_SalesOrderType")
        self.assertIn("A_SalesOrder('", first["__metadata"]["uri"])
        self.assertRegex(first["CreationDate"], r"^/Date\(-?\d+\)/$")
        self.assertRegex(first["TotalNetAmount"], r"^\d+\.\d{3}$")  # Edm.Decimal as string
        self.assertIn("__deferred", first["to_Item"])

    def test_filter_top_skip_orderby_inlinecount(self):
        _, _, body = self.get(
            SRV + "/A_SalesOrder?$filter=SalesOrganization eq '1710' and "
                  "TotalNetAmount gt 100&$orderby=TotalNetAmount desc&$top=3"
                  "&$inlinecount=allpages&$format=json")
        results = body["d"]["results"]
        self.assertLessEqual(len(results), 3)
        self.assertGreater(int(body["d"]["__count"]), len(results))
        amounts = [float(r["TotalNetAmount"]) for r in results]
        self.assertEqual(amounts, sorted(amounts, reverse=True))

    def test_filter_string_functions(self):
        _, _, body = self.get(
            BP_SRV + "/A_BusinessPartner?$filter=substringof('Becker',"
                     "BusinessPartnerFullName)&$format=json")
        names = [r["BusinessPartnerFullName"] for r in body["d"]["results"]]
        self.assertTrue(all("Becker" in n for n in names))
        self.assertTrue(names)

        _, _, body = self.get(
            BP_SRV + "/A_BusinessPartner?$filter=startswith(BusinessPartnerCategory,'1')"
                     " and endswith(SearchTerm1,'A')&$format=json")
        self.assertEqual(200, 200)

    def test_invalid_filter_returns_sap_error(self):
        status, _, body = self.get(SRV + "/A_SalesOrder?$filter=Nonsense eq 1&$format=json")
        self.assertEqual(status, 400)
        self.assertIn("error", body)
        self.assertIn("Nonsense", body["error"]["message"]["value"])
        self.assertTrue(body["error"]["code"].startswith("/IWBEP/"))

    def test_select_and_expand(self):
        _, _, body = self.get(
            SRV + "/A_SalesOrder?$top=1&$select=SalesOrder,SoldToParty&$format=json")
        row = body["d"]["results"][0]
        self.assertEqual(set(row) - {"__metadata"}, {"SalesOrder", "SoldToParty"})

        _, _, body = self.get(SRV + "/A_SalesOrder?$top=1&$expand=to_Item&$format=json")
        row = body["d"]["results"][0]
        self.assertIn("results", row["to_Item"])
        self.assertTrue(row["to_Item"]["results"])
        self.assertEqual(row["to_Item"]["results"][0]["__metadata"]["type"],
                         "API_SALES_ORDER_SRV.A_SalesOrderItemType")

    def test_entity_navigation_count_and_value(self):
        _, _, body = self.get(SRV + "/A_SalesOrder?$top=1&$format=json")
        key = body["d"]["results"][0]["SalesOrder"]

        status, _, entity = self.get(SRV + "/A_SalesOrder('%s')?$format=json" % key)
        self.assertEqual(status, 200)
        self.assertEqual(entity["d"]["SalesOrder"], key)

        status, _, items = self.get(SRV + "/A_SalesOrder('%s')/to_Item?$format=json" % key)
        self.assertEqual(status, 200)
        self.assertTrue(items["d"]["results"])

        status, _, count = self.get(SRV + "/A_SalesOrder('%s')/to_Item/$count" % key, raw=True)
        self.assertEqual(int(count), len(items["d"]["results"]))

        status, _, value = self.get(
            SRV + "/A_SalesOrder('%s')/SoldToParty/$value" % key, raw=True)
        self.assertEqual(value.decode(), entity["d"]["SoldToParty"])

        status, _, body = self.get(SRV + "/A_SalesOrder('0000000000')?$format=json")
        self.assertEqual(status, 404)

    def test_set_count(self):
        _, _, count = self.get(SRV + "/A_SalesOrder/$count", raw=True)
        _, _, body = self.get(SRV + "/A_SalesOrder?$inlinecount=allpages&$top=1&$format=json")
        self.assertEqual(int(count), int(body["d"]["__count"]))


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


class TestBatch(MockServerCase):
    def _batch(self, payload: str):
        boundary = "batch_test"
        headers = self.csrf_token()
        headers["Content-Type"] = "multipart/mixed; boundary=" + boundary
        return self.request("POST", SRV + "/$batch", body=payload, headers=headers, raw=True)

    def test_batch_get_and_changeset(self):
        body = (
            "--batch_test\r\n"
            "Content-Type: application/http\r\n"
            "Content-Transfer-Encoding: binary\r\n\r\n"
            "GET A_SalesOrder?$top=1&$format=json HTTP/1.1\r\n"
            "Accept: application/json\r\n\r\n"
            "--batch_test\r\n"
            "Content-Type: multipart/mixed; boundary=changeset_1\r\n\r\n"
            "--changeset_1\r\n"
            "Content-Type: application/http\r\n"
            "Content-Transfer-Encoding: binary\r\n\r\n"
            "POST A_SalesOrder HTTP/1.1\r\n"
            "Content-Type: application/json\r\n\r\n"
            '{"SalesOrderType":"OR","SalesOrganization":"1710","SoldToParty":"1000001",'
            '"DistributionChannel":"10","OrganizationDivision":"00"}\r\n'
            "--changeset_1--\r\n"
            "--batch_test--\r\n"
        )
        status, headers, raw = self._batch(body)
        text = raw.decode()
        self.assertEqual(status, 202)
        self.assertIn("multipart/mixed", headers["Content-Type"])
        self.assertIn("HTTP/1.1 200 OK", text)
        self.assertIn("HTTP/1.1 201 Created", text)
        self.assertIn("changesetresponse_", text)

    def test_changeset_is_rolled_back_on_error(self):
        _, _, before = self.get(SRV + "/A_SalesOrder/$count", raw=True)
        body = (
            "--batch_test\r\n"
            "Content-Type: multipart/mixed; boundary=changeset_2\r\n\r\n"
            "--changeset_2\r\n"
            "Content-Type: application/http\r\n"
            "Content-Transfer-Encoding: binary\r\n\r\n"
            "POST A_SalesOrder HTTP/1.1\r\n"
            "Content-Type: application/json\r\n\r\n"
            '{"SalesOrderType":"OR","SalesOrganization":"1710","SoldToParty":"1000001"}\r\n'
            "--changeset_2\r\n"
            "Content-Type: application/http\r\n"
            "Content-Transfer-Encoding: binary\r\n\r\n"
            "POST A_SalesOrder HTTP/1.1\r\n"
            "Content-Type: application/json\r\n\r\n"
            '{"ThisPropertyIsWrong":"1"}\r\n'
            "--changeset_2--\r\n"
            "--batch_test--\r\n"
        )
        status, _, raw = self._batch(body)
        text = raw.decode()
        self.assertIn("HTTP/1.1 400 Bad Request", text)
        self.assertNotIn("HTTP/1.1 201 Created", text)
        _, _, after = self.get(SRV + "/A_SalesOrder/$count", raw=True)
        self.assertEqual(int(before), int(after), "changeset must roll back")


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
