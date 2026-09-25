"""BAPI/RFC over both transports: JSON and SOAP, success and failure."""
from __future__ import annotations

import unittest
from xml.etree import ElementTree as ET

from support import BP_SRV, MockServerCase, SRV


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

class TestBapiBusinessErrors(MockServerCase):
    """A valid call that fails for a business reason: 200, with E in RETURN."""

    ORDER = {
        "ORDER_HEADER_IN": {"DOC_TYPE": "OR", "SALES_ORG": "1710",
                            "DISTR_CHAN": "10", "DIVISION": "00", "CURRENCY": "EUR"},
        "ORDER_PARTNERS": [{"PARTN_ROLE": "AG", "PARTN_NUMB": "0001000001"}],
        "ORDER_ITEMS_IN": [{"ITM_NUMBER": "000010", "MATERIAL": "TG11",
                            "REQ_QTY": "10", "COND_VALUE": "5000.00"}],
    }

    def behaves(self, rule):
        status, _, stored = self.request("POST", "/_mock/bapi-behaviour", body=rule)
        self.assertEqual(status, 201, stored)
        self.addCleanup(self.request, "DELETE", "/_mock/bapi-behaviour")
        return stored

    def order_count(self):
        return self.get("/_mock/state")[2]["A_SalesOrder"]

    def create(self, headers=None):
        return self.request("POST", "/sap/bc/rfc/BAPI_SALESORDER_CREATEFROMDAT2",
                            headers=headers or self.csrf_token(), body=self.ORDER)

    def test_a_valid_order_can_fail_for_a_business_reason(self):
        self.behaves({"function": "BAPI_SALESORDER_CREATEFROMDAT2", "type": "E",
                      "id": "V1", "number": "849",
                      "message": "Credit limit exceeded for customer 0000000001",
                      "count": 1})
        before = self.order_count()

        status, _, body = self.create()

        # the transport succeeded; the business did not
        self.assertEqual(status, 200, "a business error is not a transport error")
        [message] = body["RETURN"]
        self.assertEqual((message["TYPE"], message["ID"], message["NUMBER"]),
                         ("E", "V1", "849"))
        self.assertEqual(message["MESSAGE"],
                         "Credit limit exceeded for customer 0000000001")
        self.assertNotIn("SALESDOCUMENT", body, "no document, so no number for one")

        # counted from the mock's own state, not inferred from the answer above
        self.assertEqual(self.order_count(), before, "nothing was created")

    def test_the_document_the_failed_call_would_have_made_does_not_exist(self):
        headers = self.csrf_token()
        _, _, ok = self.create(headers)
        created = ok["SALESDOCUMENT"]            # what a successful call returns

        self.behaves({"function": "BAPI_SALESORDER_CREATEFROMDAT2", "type": "E",
                      "message": "Material TG11 is blocked for sales"})
        _, _, failed = self.create(headers)
        self.assertEqual(failed["RETURN"][0]["TYPE"], "E")

        # the number range did not move, so the next successful call takes the
        # number the failed one would have had
        self.request("DELETE", "/_mock/bapi-behaviour")
        _, _, after = self.create(headers)
        self.assertEqual(int(after["SALESDOCUMENT"]), int(created) + 1)

    def test_a_business_error_over_soap_is_a_response_not_a_fault(self):
        self.behaves({"function": "BAPI_MATERIAL_GET_DETAIL", "type": "E",
                      "id": "M3", "number": "305",
                      "message": "Material TG11 is not maintained in plant 1010"})
        headers = self.csrf_token()
        headers["Content-Type"] = "text/xml"
        envelope = (
            '<?xml version="1.0"?>'
            '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/">'
            "<soapenv:Body>"
            '<urn:MaterialGetDetail xmlns:urn="urn:sap-com:document:sap:soap:functions:mc-style">'
            "<Material>TG11</Material></urn:MaterialGetDetail>"
            "</soapenv:Body></soapenv:Envelope>")

        status, _, raw = self.request(
            "POST", "/sap/bc/srt/rfc/sap/materialgetdetail/100/materialgetdetail/binding",
            body=envelope, headers=headers, raw=True)

        self.assertEqual(status, 200)
        payload = raw.decode()
        self.assertNotIn("Fault", payload, "a business error is not a SOAP fault")
        self.assertIn("MaterialGetDetailResponse", payload)
        root = ET.fromstring(raw)
        types = [e.text for e in root.iter() if e.tag.endswith("TYPE")]
        messages = [e.text for e in root.iter() if e.tag.endswith("MESSAGE")]
        self.assertEqual(types, ["E"])
        self.assertEqual(messages, ["Material TG11 is not maintained in plant 1010"])

    def test_a_warning_rides_along_with_a_call_that_worked(self):
        # A client that treats any non-empty RETURN as failure has its own bug,
        # and this is how you find out.
        self.behaves({"function": "BAPI_SALESORDER_CREATEFROMDAT2", "type": "W",
                      "id": "V4", "number": "233",
                      "message": "Delivery date in the past, order accepted"})
        before = self.order_count()

        status, _, body = self.create()

        self.assertEqual(status, 200)
        self.assertRegex(body["SALESDOCUMENT"], r"^\d{10}$")
        self.assertEqual(self.order_count(), before + 1, "the order was created")
        types = [m["TYPE"] for m in body["RETURN"]]
        self.assertEqual(types, ["S", "W"], "the success message, then the warning")

        # and the order really is there
        _, _, entity = self.get(
            SRV + "/A_SalesOrder('%s')?$format=json" % body["SALESDOCUMENT"])
        self.assertEqual(entity["d"]["SalesOrder"], body["SALESDOCUMENT"])

    def test_a_rule_is_spent_after_its_count(self):
        self.behaves({"function": "BAPI_SALESORDER_CREATEFROMDAT2", "type": "E",
                      "message": "Posting period is not open", "count": 2})
        headers = self.csrf_token()

        first = self.create(headers)[2]
        second = self.create(headers)[2]
        third = self.create(headers)[2]

        self.assertEqual(first["RETURN"][0]["TYPE"], "E")
        self.assertEqual(second["RETURN"][0]["TYPE"], "E")
        self.assertEqual(third["RETURN"][0]["TYPE"], "S", "the rule was good for two")
        self.assertRegex(third["SALESDOCUMENT"], r"^\d{10}$")

    def test_only_the_named_function_is_affected(self):
        self.behaves({"function": "BAPI_PO_CREATE1", "type": "E",
                      "message": "Vendor is blocked for purchasing"})

        _, _, order = self.create()
        self.assertEqual(order["RETURN"][0]["TYPE"], "S")

    def test_a_function_with_no_return_table_is_refused(self):
        # RFC_READ_TABLE is not a BAPI: a real one raises an ABAP exception.
        status, _, body = self.request("POST", "/_mock/bapi-behaviour", body={
            "function": "RFC_READ_TABLE", "type": "E", "message": "nope"})
        self.assertEqual(status, 400)
        self.assertIn("no RETURN table", body["error"]["message"]["value"])

        status, _, body = self.request("POST", "/_mock/bapi-behaviour", body={
            "function": "BAPI_NO_SUCH_THING", "type": "E", "message": "nope"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "RFC_ERROR_FUNCTION_NOT_FOUND")

        status, _, body = self.request("POST", "/_mock/bapi-behaviour", body={
            "function": "BAPI_PO_CREATE1", "type": "X", "message": "nope"})
        self.assertEqual(status, 400)
        self.assertIn("message type", body["error"]["message"]["value"])

        status, _, body = self.request("POST", "/_mock/bapi-behaviour", body={
            "function": "BAPI_PO_CREATE1", "type": "E"})
        self.assertEqual(status, 400)
        self.assertIn("Give the message", body["error"]["message"]["value"])

        _, _, listing = self.get("/_mock/bapi-behaviour")
        self.assertEqual(listing["results"], [], "no half-formed rule was kept")
        self.assertEqual(sorted(listing["types"]), ["A", "E", "S", "W"])

    def test_reset_clears_the_behaviour_rules(self):
        self.request("POST", "/_mock/bapi-behaviour", body={
            "function": "BAPI_SALESORDER_CREATEFROMDAT2", "type": "E",
            "message": "Credit limit exceeded"})
        self.request("POST", "/_mock/reset")

        _, _, body = self.create()
        self.assertEqual(body["RETURN"][0]["TYPE"], "S")


class TestMasterDataReads(MockServerCase):
    def call(self, name, params):
        return self.request("POST", "/sap/bc/rfc/" + name, body=params,
                            headers=self.csrf_token())

    def test_customer_getlist(self):
        status, _, body = self.call("BAPI_CUSTOMER_GETLIST", {"MAXROWS": 3})
        self.assertEqual(status, 200)
        self.assertEqual(len(body["ADDRESSDATA"]), 3)
        first = body["ADDRESSDATA"][0]
        for field in ("CUSTOMER", "NAME", "CITY", "COUNTRY", "POSTL_COD1"):
            self.assertIn(field, first)
        self.assertEqual(body["RETURN"], [])

        _, _, filtered = self.call("BAPI_CUSTOMER_GETLIST", {
            "IDRANGE": [{"SIGN": "I", "OPTION": "EQ", "LOW": "1000001"}]})
        self.assertEqual([r["CUSTOMER"] for r in filtered["ADDRESSDATA"]], ["1000001"])

        _, _, empty = self.call("BAPI_CUSTOMER_GETLIST", {
            "IDRANGE": [{"SIGN": "I", "OPTION": "EQ", "LOW": "7777777"}]})
        self.assertEqual(empty["ADDRESSDATA"], [])
        self.assertEqual(empty["RETURN"][0]["TYPE"], "W")

    def test_customer_and_vendor_detail(self):
        _, _, body = self.call("BAPI_CUSTOMER_GETDETAIL2", {"CUSTOMERNO": "0001000001"})
        self.assertEqual(body["RETURN"]["TYPE"], "S")
        self.assertTrue(body["CUSTOMERGENERALDETAIL"]["NAME"])
        self.assertTrue(body["CUSTOMERGENERALDETAIL"]["CITY"])
        self.assertEqual(body["CUSTOMERCOMPANYDETAIL"]["COMP_CODE"], "1710")

        _, _, missing = self.call("BAPI_CUSTOMER_GETDETAIL2", {"CUSTOMERNO": "7777777"})
        self.assertEqual(missing["RETURN"]["TYPE"], "E")
        self.assertEqual(missing["CUSTOMERGENERALDETAIL"], {})

        # a supplier is a vendor, and a customer is not
        _, _, suppliers = self.get(
            BP_SRV + "/A_BusinessPartner?$filter=Supplier ne ''&$top=1&$format=json")
        vendor = suppliers["d"]["results"][0]["Supplier"]
        _, _, body = self.call("BAPI_VENDOR_GETDETAIL", {"VENDORNO": vendor})
        self.assertEqual(body["RETURN"]["TYPE"], "S")
        self.assertTrue(body["GENERALDETAIL"]["NAME"])

        _, _, not_a_vendor = self.call("BAPI_VENDOR_GETDETAIL", {"VENDORNO": "1000001"})
        self.assertEqual(not_a_vendor["RETURN"]["TYPE"], "E")

    def test_material_getlist(self):
        _, _, body = self.call("BAPI_MATERIAL_GETLIST", {})
        self.assertTrue(body["MATNRLIST"])
        self.assertTrue(all(m["MATL_DESC"] or m["MATL_TYPE"] for m in body["MATNRLIST"]))

        _, _, pattern = self.call("BAPI_MATERIAL_GETLIST", {"MATNRSELECTION": [
            {"SIGN": "I", "OPTION": "CP", "MATNR_LOW": "TG1*"}]})
        materials = [m["MATERIAL"] for m in pattern["MATNRLIST"]]
        self.assertTrue(materials)
        self.assertTrue(all(m.startswith("TG1") for m in materials))

        _, _, ranged = self.call("BAPI_MATERIAL_GETLIST", {"MATNRSELECTION": [
            {"SIGN": "I", "OPTION": "BT", "MATNR_LOW": "TG11", "MATNR_HIGH": "TG13"}]})
        self.assertEqual([m["MATERIAL"] for m in ranged["MATNRLIST"]],
                         ["TG11", "TG12", "TG13"])


class TestSalesOrderChange(MockServerCase):
    def call(self, params):
        return self.request("POST", "/sap/bc/rfc/BAPI_SALESORDER_CHANGE",
                            body=params, headers=self.csrf_token())

    def an_order(self):
        _, _, body = self.get(SRV + "/A_SalesOrder?$top=1&$format=json")
        return body["d"]["results"][0]["SalesOrder"]

    def field_of(self, order, name):
        _, _, body = self.get(SRV + "/A_SalesOrder('%s')?$format=json" % order)
        return body["d"][name]

    def test_nothing_changes_without_the_x_structure(self):
        order = self.an_order()
        before = self.field_of(order, "PurchaseOrderByCustomer")
        _, _, body = self.call({"SALESDOCUMENT": order,
                                "ORDER_HEADER_IN": {"PURCH_NO_C": "PO-IGNORED"}})
        self.assertEqual(body["RETURN"][0]["TYPE"], "W")
        self.assertIn("ORDER_HEADER_INX", body["RETURN"][0]["MESSAGE"])
        self.assertEqual(self.field_of(order, "PurchaseOrderByCustomer"), before,
                         "a BAPI changes only what the X structure flags")

    def test_a_flagged_field_is_changed(self):
        order = self.an_order()
        _, _, body = self.call({
            "SALESDOCUMENT": order,
            "ORDER_HEADER_IN": {"PURCH_NO_C": "PO-CHANGED", "INCOTERMS1": "CIF"},
            "ORDER_HEADER_INX": {"UPDATEFLAG": "U", "PURCH_NO_C": "X"}})
        self.assertEqual(body["RETURN"][0]["TYPE"], "S")
        self.assertEqual(self.field_of(order, "PurchaseOrderByCustomer"), "PO-CHANGED")
        self.assertNotEqual(self.field_of(order, "IncotermsClassification"), "CIF",
                            "an unflagged field is left alone")

    def test_items_can_be_inserted_updated_and_deleted(self):
        order = self.an_order()
        _, _, before = self.get(SRV + "/A_SalesOrder('%s')/to_Item/$count" % order, raw=True)

        _, _, body = self.call({
            "SALESDOCUMENT": order,
            "ORDER_ITEM_IN": [{"ITM_NUMBER": "000900", "MATERIAL": "TG22",
                               "REQ_QTY": "4", "COND_VALUE": "400"}],
            "ORDER_ITEM_INX": [{"ITM_NUMBER": "000900", "UPDATEFLAG": "I"}]})
        self.assertEqual(body["RETURN"][0]["TYPE"], "S")
        _, _, after = self.get(SRV + "/A_SalesOrder('%s')/to_Item/$count" % order, raw=True)
        self.assertEqual(int(after), int(before) + 1)

        self.call({"SALESDOCUMENT": order,
                   "ORDER_ITEM_IN": [{"ITM_NUMBER": "000900", "SHORT_TEXT": "Renamed"}],
                   "ORDER_ITEM_INX": [{"ITM_NUMBER": "000900", "UPDATEFLAG": "U",
                                       "SHORT_TEXT": "X"}]})
        _, _, item = self.get(
            SRV + "/A_SalesOrderItem(SalesOrder='%s',SalesOrderItem='000900')?$format=json"
            % order)
        self.assertEqual(item["d"]["SalesOrderItemText"], "Renamed")

        self.call({"SALESDOCUMENT": order,
                   "ORDER_ITEM_IN": [{"ITM_NUMBER": "000900"}],
                   "ORDER_ITEM_INX": [{"ITM_NUMBER": "000900", "UPDATEFLAG": "D"}]})
        _, _, final = self.get(SRV + "/A_SalesOrder('%s')/to_Item/$count" % order, raw=True)
        self.assertEqual(int(final), int(before))

    def test_failures(self):
        _, _, body = self.call({"SALESDOCUMENT": "9999999999"})
        self.assertEqual(body["RETURN"][0]["TYPE"], "E")

        order = self.an_order()
        _, _, body = self.call({
            "SALESDOCUMENT": order,
            "ORDER_ITEM_IN": [{"ITM_NUMBER": "009999", "SHORT_TEXT": "x"}],
            "ORDER_ITEM_INX": [{"ITM_NUMBER": "009999", "UPDATEFLAG": "U",
                                "SHORT_TEXT": "X"}]})
        self.assertEqual(body["RETURN"][0]["TYPE"], "E")
        self.assertIn("009999", body["RETURN"][0]["MESSAGE"])


class TestReadTable(MockServerCase):
    def call(self, params):
        return self.request("POST", "/sap/bc/rfc/RFC_READ_TABLE",
                            body=params, headers=self.csrf_token())

    def test_delimited_read(self):
        status, _, body = self.call({
            "QUERY_TABLE": "A_SalesOrder", "DELIMITER": "|", "ROWCOUNT": 2,
            "FIELDS": [{"FIELDNAME": "SalesOrder"}, {"FIELDNAME": "TotalNetAmount"},
                       {"FIELDNAME": "CreationDate"}]})
        self.assertEqual(status, 200)
        self.assertEqual([f["FIELDNAME"] for f in body["FIELDS"]],
                         ["SalesOrder", "TotalNetAmount", "CreationDate"])
        self.assertEqual([f["TYPE"] for f in body["FIELDS"]], ["C", "P", "D"])
        self.assertEqual(len(body["DATA"]), 2)
        cells = body["DATA"][0]["WA"].split("|")
        self.assertEqual(len(cells), 3)
        self.assertRegex(cells[2], r"^\d{8}$", "a date is yyyymmdd in a work area")

    def test_fixed_width_read_matches_the_field_layout(self):
        _, _, body = self.call({
            "QUERY_TABLE": "A_Product", "ROWCOUNT": 1,
            "FIELDS": [{"FIELDNAME": "Product"}, {"FIELDNAME": "ProductType"}]})
        layout = {f["FIELDNAME"]: (int(f["OFFSET"]), int(f["LENGTH"])) for f in body["FIELDS"]}
        row = body["DATA"][0]["WA"]
        offset, length = layout["ProductType"]
        self.assertEqual(row[offset:offset + length].strip(),
                         row[offset:offset + length].strip())
        self.assertEqual(len(row), sum(l for _o, l in layout.values()))

    def test_options_are_a_where_clause(self):
        _, _, body = self.call({
            "QUERY_TABLE": "A_SalesOrder", "DELIMITER": ";",
            "FIELDS": [{"FIELDNAME": "SalesOrder"}, {"FIELDNAME": "SalesOrganization"}],
            "OPTIONS": [{"TEXT": "SalesOrganization = '1710' AND TotalNetAmount > '1000'"}]})
        self.assertTrue(body["DATA"])
        for row in body["DATA"]:
            self.assertEqual(row["WA"].split(";")[1], "1710")

    def test_no_data_returns_the_layout_only(self):
        _, _, body = self.call({"QUERY_TABLE": "A_Product", "NO_DATA": "X"})
        self.assertEqual(body["DATA"], [])
        self.assertTrue(body["FIELDS"])

    def test_paging(self):
        _, _, first = self.call({"QUERY_TABLE": "A_SalesOrder", "ROWCOUNT": 1,
                                 "DELIMITER": "|", "FIELDS": [{"FIELDNAME": "SalesOrder"}]})
        _, _, second = self.call({"QUERY_TABLE": "A_SalesOrder", "ROWCOUNT": 1,
                                  "ROWSKIPS": 1, "DELIMITER": "|",
                                  "FIELDS": [{"FIELDNAME": "SalesOrder"}]})
        self.assertNotEqual(first["DATA"][0]["WA"], second["DATA"][0]["WA"])

    def test_it_refuses_what_it_does_not_know(self):
        status, _, body = self.call({"QUERY_TABLE": "A_Nonsense"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "TABLE_NOT_AVAILABLE")

        # a classic table name gets a pointer rather than a shrug
        status, _, body = self.call({"QUERY_TABLE": "VBAK"})
        self.assertEqual(status, 400)
        self.assertIn("A_SalesOrder", body["error"]["message"]["value"])

        status, _, body = self.call({"QUERY_TABLE": "A_Product",
                                     "FIELDS": [{"FIELDNAME": "Nonsense"}]})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "FIELD_NOT_VALID")

    def test_options_cannot_smuggle_sql(self):
        for text in ("DROP TABLE A_Product",
                     "Product = 'x'; DELETE FROM A_Product",
                     "1=1 UNION SELECT * FROM A_SalesOrder"):
            status, _, body = self.call({"QUERY_TABLE": "A_Product",
                                         "OPTIONS": [{"TEXT": text}]})
            self.assertEqual(status, 400, text)
            self.assertIn(body["error"]["code"],
                          ("FIELD_NOT_VALID", "OPTION_NOT_VALID"), text)

        # and the table is still there
        _, _, body = self.call({"QUERY_TABLE": "A_Product", "NO_DATA": "X"})
        self.assertTrue(body["FIELDS"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
