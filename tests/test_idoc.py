"""IDoc round-trip: ORDERS05 generation, inbound XML and flat files, status."""
from __future__ import annotations

import datetime
import json
import re
import unittest
from xml.etree import ElementTree as ET

from support import MockServerCase, SRV, finsta, invoic, line


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
        # An ORDERS is filed and nothing else, so a flat one posts: the refusal
        # of a flat INVOIC below must stay narrow, not reject every flat IDoc.
        self.assertEqual(receipt["STATUS"], "53")

    def test_a_flat_invoic_is_refused_rather_than_filed_as_posted(self):
        """A flat INVOIC cannot be posted, so it must not report 53.

        Posting reads the segments, and this mock has no fixed-width layout for
        them - only the control record. The flat ORDERS05 above still posts 53
        because an ORDERS is filed and nothing else; an INVOIC would have owed
        somebody money, so silence would be the same lie as any other IDoc that
        posts nothing and says it posted.
        """
        headers = self.csrf_token()
        control = ("EDI_DC40  " + "100" + "0" * 16 + "0756" + "53" + "2" + "2" + " " + " "
                   + "INVOIC02".ljust(30) + " " * 30 + "INVOIC".ljust(30))
        status, _, receipt = self.request(
            "POST", "/sap/bc/idoc", body=control + "\nE1EDK01   ...",
            headers=dict(headers, **{"Content-Type": "text/plain"}))

        self.assertEqual(status, 201, "the IDoc was still received")
        self.assertEqual(receipt["STATUS"], "51")
        self.assertIn("flat file", receipt["STATUS_TEXT"], receipt["STATUS_TEXT"])
        self.assertNotIn("APPLIED", receipt)

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

    # ----------------------------------------------------------------- posting

    def posts_as(self, rule):
        status, _, stored = self.request("POST", "/_mock/idoc-posting", body=rule)
        self.assertEqual(status, 201)
        self.addCleanup(self.request, "DELETE", "/_mock/idoc-posting")
        return stored

    def test_an_idoc_can_be_accepted_and_not_posted(self):
        self.posts_as({"mestyp": "INVOIC", "status": "51",
                       "message": "Posting period 08 2026 is not open", "count": 1})
        headers = self.csrf_token()
        invoice = (
            '<?xml version="1.0" encoding="utf-8"?><INVOIC02><IDOC BEGIN="1">'
            '<EDI_DC40 SEGMENT="1"><IDOCTYP>INVOIC02</IDOCTYP><MESTYP>INVOIC</MESTYP>'
            "</EDI_DC40></IDOC></INVOIC02>")

        status, _, receipt = self.request(
            "POST", "/sap/bc/idoc", body=invoice,
            headers=dict(headers, **{"Content-Type": "application/xml",
                                     "Accept": "application/json"}))

        # received is not posted: the port took it, the application refused it
        self.assertEqual(status, 201, "the IDoc was received, so 201 stands")
        self.assertEqual(receipt["STATUS"], "51")
        self.assertEqual(receipt["STATUS_TEXT"], "Posting period 08 2026 is not open")
        self.assertRegex(receipt["DOCNUM"], r"^\d{16}$")

        # and it is readable afterwards, the way a failed IDoc is in WE02
        _, _, record = self.get("/sap/bc/idoc/" + receipt["DOCNUM"])
        self.assertEqual((record["status"], record["direction"]), ("51", "2"))
        self.assertEqual(record["status_text"], "Posting period 08 2026 is not open")

    def test_a_delivery_that_does_not_post_leaves_the_order_alone(self):
        """The difference between 53 and 51 is what happened to the order."""
        headers = self.csrf_token()
        _, _, created = self.request("POST", SRV + "/A_SalesOrder", headers=headers, body={
            "SalesOrderType": "OR", "SalesOrganization": "1710", "SoldToParty": "1000001",
            "DistributionChannel": "10", "OrganizationDivision": "00",
            "to_Item": [{"Material": "TG11", "RequestedQuantity": "4",
                         "RequestedQuantityUnit": "PC", "NetAmount": "400"}]})
        order = created["d"]["SalesOrder"]

        # read the order's state from the service rather than assuming it
        _, _, before = self.get(SRV + "/A_SalesOrder('%s')?$format=json" % order)
        deliveries_before = self.get(
            "/sap/opu/odata/sap/API_OUTBOUND_DELIVERY_SRV/A_OutbDeliveryHeader"
            "?$format=json")[2]["d"]["results"]

        self.posts_as({"mestyp": "DELVRY", "status": "51",
                       "message": "Goods movement not possible for plant 1710"})
        delivery = (
            '<?xml version="1.0" encoding="utf-8"?><DELVRY07><IDOC BEGIN="1">'
            '<EDI_DC40 SEGMENT="1"><IDOCTYP>DELVRY07</IDOCTYP><MESTYP>DELVRY</MESTYP>'
            "</EDI_DC40>"
            '<E1EDL20 SEGMENT="1"><VBELN>0080007788</VBELN>'
            '<E1EDL24 SEGMENT="1"><POSNR>000010</POSNR><MATNR>TG11</MATNR>'
            "<LFIMG>4.000</LFIMG><VGBEL>%s</VGBEL><VGPOS>000010</VGPOS></E1EDL24>"
            "</E1EDL20></IDOC></DELVRY07>") % order

        status, _, receipt = self.request(
            "POST", "/sap/bc/idoc", body=delivery,
            headers=dict(headers, **{"Content-Type": "application/xml",
                                     "Accept": "application/json"}))
        self.assertEqual(status, 201)
        self.assertEqual(receipt["STATUS"], "51")
        self.assertNotIn("APPLIED", receipt, "nothing was applied, so say nothing")

        # the order is where it was: still not delivered, no delivery created
        _, _, after = self.get(SRV + "/A_SalesOrder('%s')?$format=json" % order)
        self.assertEqual(after["d"]["OverallDeliveryStatus"],
                         before["d"]["OverallDeliveryStatus"])
        self.assertEqual(after["d"]["OverallDeliveryStatus"], "A")
        deliveries_after = self.get(
            "/sap/opu/odata/sap/API_OUTBOUND_DELIVERY_SRV/A_OutbDeliveryHeader"
            "?$format=json")[2]["d"]["results"]
        self.assertEqual(len(deliveries_after), len(deliveries_before))

    def test_a_field_too_long_for_sap_is_status_51_not_a_400(self):
        """The IDoc surface answers in status records, including for this (#101).

        A 17-character reference used to come back as HTTP 400 with a Gateway
        error envelope - from the one surface whose contract is that a posting
        failure arrives as a status record on a 201. So the failure most
        likely to hit a client in production was the case that never
        exercised its status-record handling.
        """
        reference = "REF-1234567890123"
        self.assertEqual(len(reference), 17)
        status, _, receipt = self.request(
            "POST", "/sap/bc/idoc", body=invoic(reference),
            headers=dict(self.csrf_token(), **{"Content-Type": "application/xml",
                                               "Accept": "application/json"}))
        self.assertEqual(status, 201, "the IDoc was received and filed")
        self.assertEqual(receipt["STATUS"], "51")
        self.assertIn("ReferenceDocument", receipt["STATUS_TEXT"])
        self.assertIn("16", receipt["STATUS_TEXT"], "the limit, not just the field")
        self.assertRegex(receipt["DOCNUM"], r"^\d{16}$", "a docnum was issued")

        # and the same value over OData is still a 400, where it belongs
        status, _, body = self.request(
            "POST", SRV + "/A_SalesOrder", headers=self.csrf_token(),
            body={"SalesOrderType": "OR", "SalesOrganization": "1710",
                  "SoldToParty": "1000001", "DistributionChannel": "10",
                  "OrganizationDivision": "00", "TransactionCurrency": "EUR",
                  "PurchaseOrderByCustomer": "x" * 40})
        self.assertEqual(status, 400)

    def test_a_delvry_that_delivers_nothing_is_not_posted(self):
        """A DELVRY naming no order line is 51, not 53.

        Same argument as the INVOIC: 53 is *Application document posted*, and
        an IDoc with no E1EDL24 naming a document and position moves no order,
        so claiming it posted leaves a client nothing to check.
        """
        headers = dict(self.csrf_token(), **{"Content-Type": "application/xml",
                                             "Accept": "application/json"})
        delivery = (
            '<?xml version="1.0" encoding="utf-8"?><DELVRY07><IDOC BEGIN="1">'
            '<EDI_DC40 SEGMENT="1"><IDOCTYP>DELVRY07</IDOCTYP><MESTYP>DELVRY</MESTYP>'
            "</EDI_DC40>"
            '<E1EDL20 SEGMENT="1"><VBELN>0080007778</VBELN></E1EDL20>'
            "</IDOC></DELVRY07>")

        status, _, receipt = self.request("POST", "/sap/bc/idoc", body=delivery,
                                         headers=headers)

        self.assertEqual(status, 201)
        self.assertEqual(receipt["STATUS"], "51")
        self.assertIn("E1EDL24", receipt["STATUS_TEXT"], receipt["STATUS_TEXT"])
        self.assertNotIn("APPLIED", receipt)

    def test_an_orders_idoc_with_nothing_to_post_is_still_53(self):
        """The limit of the rule above: ORDERS has no application step at all.

        An ORDERS05 is filed and nothing else, so there is nothing for it to
        decline and 53 is honest. Without this, "post before filing" would
        drift into refusing every IDoc that does not create a document.
        """
        headers = dict(self.csrf_token(), **{"Content-Type": "application/xml",
                                             "Accept": "application/json"})
        order = ('<?xml version="1.0" encoding="utf-8"?><ORDERS05><IDOC BEGIN="1">'
                 '<EDI_DC40 SEGMENT="1"><IDOCTYP>ORDERS05</IDOCTYP>'
                 "<MESTYP>ORDERS</MESTYP></EDI_DC40></IDOC></ORDERS05>")

        receipt = self.request("POST", "/sap/bc/idoc", body=order, headers=headers)[2]

        self.assertEqual(receipt["STATUS"], "53")
        self.assertEqual(receipt["STATUS_TEXT"], "Application document posted")

    def test_a_rule_can_be_spent_leaving_the_retry_to_post(self):
        self.posts_as({"mestyp": "ORDERS", "status": "51",
                       "message": "Customer 1000001 is blocked for orders",
                       "count": 1})
        headers = dict(self.csrf_token(), **{"Content-Type": "application/xml",
                                             "Accept": "application/json"})
        order_idoc = (
            '<?xml version="1.0" encoding="utf-8"?><ORDERS05><IDOC BEGIN="1">'
            '<EDI_DC40 SEGMENT="1"><IDOCTYP>ORDERS05</IDOCTYP><MESTYP>ORDERS</MESTYP>'
            "</EDI_DC40></IDOC></ORDERS05>")

        first = self.request("POST", "/sap/bc/idoc", body=order_idoc, headers=headers)[2]
        second = self.request("POST", "/sap/bc/idoc", body=order_idoc, headers=headers)[2]

        self.assertEqual(first["STATUS"], "51")
        self.assertEqual(second["STATUS"], "53", "the rule was good for one IDoc")
        self.assertEqual(second["STATUS_TEXT"], "Application document posted")
        self.assertNotEqual(first["DOCNUM"], second["DOCNUM"])

    def test_a_rule_only_catches_the_message_type_it_names(self):
        self.posts_as({"mestyp": "INVOIC", "status": "68"})
        headers = dict(self.csrf_token(), **{"Content-Type": "application/xml",
                                             "Accept": "application/json"})

        def send(idoctyp, mestyp):
            body = ('<?xml version="1.0" encoding="utf-8"?><%s><IDOC BEGIN="1">'
                    '<EDI_DC40 SEGMENT="1"><IDOCTYP>%s</IDOCTYP><MESTYP>%s</MESTYP>'
                    "</EDI_DC40></IDOC></%s>") % (idoctyp, idoctyp, mestyp, idoctyp)
            return self.request("POST", "/sap/bc/idoc", body=body, headers=headers)[2]

        self.assertEqual(send("INVOIC02", "INVOIC")["STATUS"], "68")
        self.assertEqual(send("ORDERS05", "ORDERS")["STATUS"], "53")

    def test_a_status_posting_cannot_end_in_is_refused(self):
        # 12 is a dispatch status: an outbound IDoc on its way to a port.
        status, _, body = self.request(
            "POST", "/_mock/idoc-posting", body={"mestyp": "ORDERS", "status": "12"})
        self.assertEqual(status, 400)
        self.assertIn("cannot end in status 12", body["error"]["message"]["value"])

        # and nothing was stored, so the next IDoc still posts
        _, _, listing = self.get("/_mock/idoc-posting")
        self.assertEqual(listing["results"], [])
        self.assertEqual(sorted(listing["statuses"]), ["51", "53", "56", "68"])

    def test_reset_clears_the_posting_rules(self):
        self.request("POST", "/_mock/idoc-posting", body={"status": "51"})
        self.request("POST", "/_mock/reset")

        headers = dict(self.csrf_token(), **{"Content-Type": "application/xml",
                                             "Accept": "application/json"})
        _, _, receipt = self.request(
            "POST", "/sap/bc/idoc", headers=headers,
            body='<?xml version="1.0" encoding="utf-8"?><ORDERS05><IDOC BEGIN="1">'
                 '<EDI_DC40 SEGMENT="1"><IDOCTYP>ORDERS05</IDOCTYP>'
                 "<MESTYP>ORDERS</MESTYP></EDI_DC40></IDOC></ORDERS05>")
        self.assertEqual(receipt["STATUS"], "53")

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


class TestTellingASupplierWhatWasPaid(MockServerCase):
    """A REMADV generated from the clearing a statement posted (#106).

    The one generator whose source is not a sales order. mock-edi 0.7.0 ships
    the receiving half, and two of its findings are aimed at a payer that gets
    this wrong: a total that is not the sum of its parts, and an advice that
    claims a settlement the payment has not reached.
    """

    CUBE = ("/sap/opu/odata/sap/API_OPLACCTGDOCITEMCUBE_SRV"
            "/A_OperationalAcctgDocItemCube")

    # -- helpers

    def send(self, body):
        headers = dict(self.csrf_token(), **{"Content-Type": "application/xml",
                                             "Accept": "application/json"})
        status, _, receipt = self.request("POST", "/sap/bc/idoc", body=body,
                                          headers=headers)
        self.assertEqual(status, 201)
        return receipt

    def bill(self, reference, gross="1190.00", supplier="1000009"):
        net = "%.2f" % (float(gross) / 1.19)
        tax = "%.2f" % (float(gross) - float(net))
        return self.send(invoic(reference, gross, net, tax,
                                supplier))["APPLIED"][0]

    def advise(self, clearing_document):
        return self.request(
            "POST", "/sap/bc/idoc/generate",
            body={"mestyp": "REMADV",
                  "ClearingAccountingDocument": clearing_document},
            headers=self.csrf_token())

    def a_payment_of(self, *invoices, **kwargs):
        """Pay these invoices with one statement, and return the clearing."""
        statement = kwargs.pop("statement", "00080")
        lines = "".join(
            line("%06d" % (n + 1), "%.2f-" % float(gross), reference=reference)
            for n, (reference, gross) in enumerate(invoices))
        applied = self.send(finsta(lines, statement=statement))["APPLIED"][0]
        self.assertEqual(len(applied["CLEARED"]), len(invoices),
                         applied["UNPROCESSED"])
        documents = {row["CLEARINGDOCUMENT"] for row in applied["CLEARED"]}
        self.assertEqual(len(documents), 1, "one supplier, one payment")
        return documents.pop()

    def xml_of(self, response):
        status, _, out = response
        self.assertEqual(status, 201, out)
        return out, ET.fromstring(out["xml"])

    def eight_digits(self, odata_date):
        """`/Date(ms)/` as the eight digits an IDoc date carries."""
        ms = int(re.search(r"-?\d+", odata_date).group())
        return (datetime.datetime(1970, 1, 1)
                + datetime.timedelta(milliseconds=ms)).strftime("%Y%m%d")

    # -- tests

    def test_an_advice_names_every_invoice_the_payment_settled(self):
        self.bill("ADV-A1", "1190.00")
        self.bill("ADV-A2", "2380.00")
        paying = self.a_payment_of(("ADV-A1", "1190.00"), ("ADV-A2", "2380.00"),
                                   statement="00081")

        out, root = self.xml_of(self.advise(paying))

        self.assertEqual(root.tag, "PEXR2002")
        self.assertEqual(root.find(".//EDI_DC40/IDOCTYP").text, "PEXR2002")
        self.assertEqual(root.find(".//EDI_DC40/MESTYP").text, "REMADV")
        rows = root.findall(".//E1IDPU1")
        self.assertEqual(len(rows), 2)
        self.assertEqual([r.find("E1EDP02/BELNR").text for r in rows],
                         ["ADV-A1", "ADV-A2"])
        self.assertEqual(out["invoices"], ["ADV-A1", "ADV-A2"])
        self.assertEqual(out["payee"], "1000009")

    def test_the_total_is_the_sum_of_the_rows(self):
        """mock-edi's `remittance-total-not-parts`, which #107 made reachable."""
        self.bill("ADV-B1", "1190.00")
        self.bill("ADV-B2", "2380.00")
        paying = self.a_payment_of(("ADV-B1", "1190.00"), ("ADV-B2", "2380.00"),
                                   statement="00082")

        out, root = self.xml_of(self.advise(paying))

        header = root.find("./IDOC/E1IDKU1/E1IDPU5/MOABETR").text
        rows = [r.find("E1IDPU5/MOABETR").text
                for r in root.findall(".//E1IDPU1")]
        self.assertEqual(rows, ["1190.00-", "2380.00-"])
        self.assertEqual(header, "3570.00-")
        self.assertEqual(float(header[:-1]),
                         round(sum(float(x[:-1]) for x in rows), 2))
        self.assertEqual(out["total"], "3570.00")

    def test_the_advice_is_dated_by_the_payment_it_advises(self):
        """mock-edi's `remitted-before-settlement` cannot fire: the same date."""
        invoice = self.bill("ADV-C1", "1190.00")
        paying = self.a_payment_of(("ADV-C1", "1190.00"), statement="00083")

        _, root = self.xml_of(self.advise(paying))

        _, _, item = self.get(
            self.CUBE + "?$filter=AccountingDocument%%20eq%%20'%s'%%20and%%20"
            "AccountingDocumentItemType%%20eq%%20'K'&$format=json"
            % invoice["ACCOUNTINGDOCUMENT"])
        cleared = item["d"]["results"][0]["ClearingDate"]
        self.assertTrue(cleared, "the item was cleared on a day")
        self.assertEqual(root.find("./IDOC/E1IDKU1/E1EDK03/DATUM").text,
                         self.eight_digits(cleared),
                         "an advice cannot be dated after the settlement")

    def test_the_advice_quotes_the_payment_as_its_trace(self):
        self.bill("ADV-D1", "1190.00")
        paying = self.a_payment_of(("ADV-D1", "1190.00"), statement="00084")

        out, root = self.xml_of(self.advise(paying))

        self.assertEqual(root.find("./IDOC/E1IDKU1/BGMREF").text, paying)
        self.assertEqual(out["clearing_document"], paying)
        self.assertEqual(root.find("./IDOC/E1IDKU1/E1EDKA1/LIFNR").text, "1000009")
        self.assertEqual(root.find("./IDOC/E1IDKU1/E1EDKA1/PARVW").text, "LF")

    def test_one_invoice_still_makes_an_advice_that_adds_up(self):
        """The feature does not depend on #107's grouping to be correct."""
        self.bill("ADV-E1", "500.00")
        paying = self.a_payment_of(("ADV-E1", "500.00"), statement="00085")

        _, root = self.xml_of(self.advise(paying))

        rows = root.findall(".//E1IDPU1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(root.find("./IDOC/E1IDKU1/E1IDPU5/MOABETR").text, "500.00-")
        self.assertEqual(rows[0].find("E1IDPU5/MOABETR").text, "500.00-")

    def test_the_advice_is_stored_as_an_outbound_idoc(self):
        self.bill("ADV-F1", "1190.00")
        paying = self.a_payment_of(("ADV-F1", "1190.00"), statement="00086")

        out, _ = self.xml_of(self.advise(paying))

        _, _, stored = self.get("/sap/bc/idoc/%s" % out["docnum"])
        self.assertEqual(stored["idoctyp"], "PEXR2002")
        self.assertEqual(stored["mestyp"], "REMADV")
        self.assertEqual(stored["direction"], "1", "outbound")
        self.assertNotIn("APPLIED", stored, "generating one posts nothing")

    def test_a_document_that_settled_nothing_is_refused(self):
        invoice = self.bill("ADV-G1", "1190.00")

        status, _, body = self.advise(invoice["ACCOUNTINGDOCUMENT"])

        self.assertEqual(status, 400, body)
        self.assertIn("settled nothing", json.dumps(body))

    def test_a_document_that_does_not_exist_is_refused(self):
        status, _, body = self.advise("0199999999")

        self.assertEqual(status, 404, body)
        self.assertIn("does not exist", json.dumps(body))

    def test_asking_for_an_advice_without_a_payment_says_what_is_missing(self):
        """The route no longer assumes every generator wants a sales order."""
        status, _, body = self.request(
            "POST", "/sap/bc/idoc/generate",
            body={"mestyp": "REMADV", "SalesOrder": "0000000001"},
            headers=self.csrf_token())

        self.assertEqual(status, 400, body)
        self.assertIn("ClearingAccountingDocument", json.dumps(body))

    def test_an_unknown_message_type_is_still_refused_by_name(self):
        status, _, body = self.request(
            "POST", "/sap/bc/idoc/generate", body={"mestyp": "PAYEXT"},
            headers=self.csrf_token())

        self.assertEqual(status, 400, body)
        said = json.dumps(body)
        self.assertIn("PAYEXT", said)
        self.assertIn("PEXR2002", said, "and lists what it can generate")


if __name__ == "__main__":
    unittest.main(verbosity=2)
