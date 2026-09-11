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

class TestInvoiceAndDelivery(MockServerCase):
    def an_order(self):
        _, _, body = self.get(SRV + "/A_SalesOrder?$top=1&$format=json")
        return body["d"]["results"][0]["SalesOrder"]

    def generate(self, mestyp, order, **extra):
        payload = {"SalesOrder": order}
        if mestyp:
            payload["mestyp"] = mestyp
        payload.update(extra)
        return self.request("POST", "/sap/bc/idoc/generate", body=payload,
                            headers=self.csrf_token())

    def test_invoic02(self):
        order = self.an_order()
        status, _, body = self.generate("INVOIC", order)
        self.assertEqual(status, 201)
        self.assertRegex(body["billing_document"], r"^\d{10}$")

        root = ET.fromstring(body["xml"])
        self.assertEqual(root.tag, "INVOIC02")
        self.assertEqual(root.find(".//EDI_DC40/IDOCTYP").text, "INVOIC02")
        self.assertEqual(root.find(".//EDI_DC40/MESTYP").text, "INVOIC")

        for segment in ("E1EDK01", "E1EDK02", "E1EDK03", "E1EDKA1", "E1EDP01",
                        "E1EDP19", "E1EDP26", "E1EDS01"):
            self.assertTrue(root.findall(".//" + segment), segment)

        # the reference back to the order, and the partner roles a bill carries
        qualifiers = [e.find("QUALF").text for e in root.findall(".//E1EDK02")]
        self.assertIn("009", qualifiers)
        self.assertIn("001", qualifiers)
        roles = [e.find("PARVW").text for e in root.findall(".//E1EDKA1")]
        self.assertEqual(set(roles), {"RE", "RG", "AG"})

        totals = {e.find("SUMID").text: float(e.find("SUMME").text)
                  for e in root.findall(".//E1EDS01")}
        self.assertAlmostEqual(totals["010"], totals["011"] + totals["205"], places=2)

        _, _, entity = self.get(SRV + "/A_SalesOrder('%s')?$format=json" % order)
        self.assertAlmostEqual(totals["011"], float(entity["d"]["TotalNetAmount"]), places=2)

    def test_delvry07(self):
        order = self.an_order()
        status, _, body = self.generate("DELVRY", order)
        self.assertEqual(status, 201)
        self.assertRegex(body["delivery"], r"^\d{10}$")

        root = ET.fromstring(body["xml"])
        self.assertEqual(root.tag, "DELVRY07")
        header = root.find(".//E1EDL20")
        self.assertIsNotNone(header)
        self.assertEqual(header.find("VBELN").text, body["delivery"])
        self.assertTrue(root.findall(".//E1EDL21"))
        self.assertTrue(root.findall(".//E1EDL22"))
        self.assertTrue(root.findall(".//E1ADRM1"))

        items = root.findall(".//E1EDL24")
        self.assertTrue(items)
        for item in items:
            self.assertEqual(item.find("VGBEL").text, order,
                             "an item names the document it came from")
            self.assertTrue(item.find("LFIMG").text)

    def test_posting_a_delivery_moves_the_order(self):
        # a fresh order, so no earlier test in this class has delivered it
        headers = self.csrf_token()
        _, _, created = self.request("POST", SRV + "/A_SalesOrder", headers=headers, body={
            "SalesOrderType": "OR", "SalesOrganization": "1710", "SoldToParty": "1000001",
            "DistributionChannel": "10", "OrganizationDivision": "00",
            "to_Item": [{"Material": "TG11", "RequestedQuantity": "4",
                         "RequestedQuantityUnit": "PC", "NetAmount": "400"}]})
        order = created["d"]["SalesOrder"]
        self.assertEqual(created["d"]["OverallDeliveryStatus"], "A")

        delivery = (
            '<?xml version="1.0" encoding="utf-8"?><DELVRY07><IDOC BEGIN="1">'
            '<EDI_DC40 SEGMENT="1"><IDOCTYP>DELVRY07</IDOCTYP><MESTYP>DELVRY</MESTYP>'
            "</EDI_DC40>"
            '<E1EDL20 SEGMENT="1"><VBELN>0080007777</VBELN>'
            '<E1EDL24 SEGMENT="1"><POSNR>000010</POSNR><MATNR>TG11</MATNR>'
            "<LFIMG>4.000</LFIMG><VGBEL>%s</VGBEL><VGPOS>000010</VGPOS></E1EDL24>"
            "</E1EDL20></IDOC></DELVRY07>") % order

        status, _, receipt = self.request(
            "POST", "/sap/bc/idoc", body=delivery,
            headers=dict(headers, **{"Content-Type": "application/xml",
                                     "Accept": "application/json"}))
        self.assertEqual(status, 201)
        self.assertEqual(receipt["STATUS"], "53")
        applied = receipt["APPLIED"][0]
        self.assertEqual(applied["SALESORDER"], order)
        self.assertEqual(applied["STATUS"], "C")

        _, _, entity = self.get(SRV + "/A_SalesOrder('%s')?$format=json" % order)
        self.assertEqual(entity["d"]["OverallDeliveryStatus"], "C")

        # the delivery the IDoc announced was unknown, so one was created for it
        self.assertIn("DELIVERY", applied)
        _, _, delivery_entity = self.get(
            "/sap/opu/odata/sap/API_OUTBOUND_DELIVERY_SRV/A_OutbDeliveryHeader('%s')"
            "?$expand=to_DeliveryDocumentItem&$format=json" % applied["DELIVERY"])
        items = delivery_entity["d"]["to_DeliveryDocumentItem"]["results"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["ReferenceSDDocument"], order)

    def test_a_partial_delivery_says_so(self):
        order = self.an_order()
        _, _, items = self.get(SRV + "/A_SalesOrder('%s')/to_Item?$format=json" % order)
        first = items["d"]["results"][0]

        partial = (
            '<?xml version="1.0" encoding="utf-8"?><DELVRY07><IDOC BEGIN="1">'
            '<EDI_DC40 SEGMENT="1"><TABNAM>EDI_DC40</TABNAM><MANDT>100</MANDT>'
            "<IDOCTYP>DELVRY07</IDOCTYP><MESTYP>DELVRY</MESTYP></EDI_DC40>"
            '<E1EDL20 SEGMENT="1"><VBELN>0080009999</VBELN>'
            '<E1EDL24 SEGMENT="1"><POSNR>%s</POSNR><MATNR>%s</MATNR>'
            "<LFIMG>1.000</LFIMG><VGBEL>%s</VGBEL><VGPOS>%s</VGPOS></E1EDL24>"
            "</E1EDL20></IDOC></DELVRY07>"
        ) % (first["SalesOrderItem"], first["Material"], order, first["SalesOrderItem"])

        _, _, receipt = self.request(
            "POST", "/sap/bc/idoc", body=partial,
            headers=dict(self.csrf_token(), **{"Content-Type": "application/xml",
                                               "Accept": "application/json"}))
        self.assertEqual(receipt["APPLIED"][0]["STATUS"], "B")
        self.assertIn("partly", receipt["APPLIED"][0]["MESSAGE"])

        _, _, entity = self.get(SRV + "/A_SalesOrder('%s')?$format=json" % order)
        self.assertEqual(entity["d"]["OverallDeliveryStatus"], "B")

    def test_a_delivery_for_an_unknown_order_is_reported_not_applied(self):
        unknown = (
            '<?xml version="1.0"?><DELVRY07><IDOC BEGIN="1">'
            "<EDI_DC40 SEGMENT=\"1\"><IDOCTYP>DELVRY07</IDOCTYP><MESTYP>DELVRY</MESTYP>"
            "</EDI_DC40>"
            '<E1EDL24 SEGMENT="1"><POSNR>000010</POSNR><LFIMG>1.000</LFIMG>'
            "<VGBEL>9999999999</VGBEL><VGPOS>000010</VGPOS></E1EDL24>"
            "</IDOC></DELVRY07>")
        status, _, receipt = self.request(
            "POST", "/sap/bc/idoc", body=unknown,
            headers=dict(self.csrf_token(), **{"Content-Type": "application/xml",
                                               "Accept": "application/json"}))
        self.assertEqual(status, 201, "the IDoc is still filed")
        self.assertEqual(receipt["APPLIED"][0]["STATUS"], "")
        self.assertIn("does not exist", receipt["APPLIED"][0]["MESSAGE"])

    def test_message_type_selection(self):
        order = self.an_order()

        # no message type still means ORDERS05
        _, _, body = self.generate(None, order)
        self.assertEqual(ET.fromstring(body["xml"]).tag, "ORDERS05")

        # and it can come from the query string
        status, _, body = self.request(
            "POST", "/sap/bc/idoc/generate?mestyp=INVOIC",
            body={"SalesOrder": order}, headers=self.csrf_token())
        self.assertEqual(status, 201)
        self.assertEqual(ET.fromstring(body["xml"]).tag, "INVOIC02")

        status, _, error = self.generate("WHMSUOM", order)
        self.assertEqual(status, 400)
        message = error["error"]["message"]["value"]
        for known in ("ORDERS05", "INVOIC02", "DELVRY07"):
            self.assertIn(known, message)

        status, _, _ = self.generate("INVOIC", "9999999999")
        self.assertEqual(status, 404)

    def test_generated_idocs_are_filed(self):
        order = self.an_order()
        for mestyp in ("INVOIC", "DELVRY"):
            self.generate(mestyp, order)
        _, _, listing = self.get("/_mock/idocs?limit=10")
        types = {record["idoctyp"] for record in listing["results"]}
        self.assertTrue({"INVOIC02", "DELVRY07"} <= types)

        _, _, invoices = self.get("/_mock/idocs?mestyp=INVOIC")
        self.assertTrue(invoices["results"])
        self.assertTrue(all(r["mestyp"] == "INVOIC" for r in invoices["results"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
