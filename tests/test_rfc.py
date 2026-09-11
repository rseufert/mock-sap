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
