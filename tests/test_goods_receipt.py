"""A goods receipt, and the invoice that quotes one.

Under goods-receipt-based invoice verification a supplier's invoice does not
point at the purchase order alone: it names the delivery note of the shipment
it bills, SAP finds the goods receipt by that note, and what it keeps on the
invoice item is the *material document* that receipt posted - `RSEG-LFBNR`,
`LFGJA` and `LFPOS`, served as `ReferenceDocument` and friends. The delivery
note number itself belongs to the material document's header, `MKPF-XBLNR`,
and is nowhere on the invoice (#193).

So these assert the chain in both directions: a receipt that can be posted and
read, and an invoice that resolves one - or is refused for naming a receipt
that does not exist, which is the failure mock-acme asked for, since an invoice
quoting a shipment nobody received is not something to pay.

**What this mock deliberately does not do** is judge the receipt. No quantity
is compared with what was ordered, and nothing checks the invoice against
either the order or the receipt. That is the payer's job and mock-acme's
`invoice_check.py` does it; the division is the same one the README states for
the purchase order.
"""
from __future__ import annotations

import json
import unittest

from support import MockServerCase

MD = "/sap/opu/odata/sap/API_MATERIAL_DOCUMENT_SRV"
SRV = "/sap/opu/odata/sap/API_SUPPLIERINVOICE_PROCESS_SRV"
CUBE = ("/sap/opu/odata/sap/API_OPLACCTGDOCITEMCUBE_SRV"
        "/A_OperationalAcctgDocItemCube")


def invoic(reference="SUP-GR-0001", supplier="1000009", gross="1190.00",
           net="1000.00", tax="190.00", date="20260926",
           purchase_order="4500000100", position="00010",
           delivery_note=None):
    """An INVOIC whose item optionally quotes a delivery note (QUALF 016)."""
    quoted = ""
    if delivery_note is not None:
        quoted = ('<E1EDP02 SEGMENT="1"><QUALF>016</QUALF>'
                  "<BELNR>%s</BELNR></E1EDP02>" % delivery_note)
    return (
        '<?xml version="1.0" encoding="utf-8"?><INVOIC02><IDOC BEGIN="1">'
        '<EDI_DC40 SEGMENT="1"><IDOCTYP>INVOIC02</IDOCTYP>'
        "<MESTYP>INVOIC</MESTYP></EDI_DC40>"
        '<E1EDK01 SEGMENT="1"><CURCY>EUR</CURCY><ZTERM>NT30</ZTERM>'
        "<BELNR>%s</BELNR><BSART>INVO</BSART></E1EDK01>"
        '<E1EDK02 SEGMENT="1"><QUALF>009</QUALF><BELNR>%s</BELNR></E1EDK02>'
        '<E1EDK03 SEGMENT="1"><IDDAT>026</IDDAT><DATUM>%s</DATUM></E1EDK03>'
        '<E1EDKA1 SEGMENT="1"><PARVW>LF</PARVW><PARTN>%s</PARTN>'
        "<LIFNR>%s</LIFNR></E1EDKA1>"
        '<E1EDP01 SEGMENT="1"><POSEX>000010</POSEX><MENGE>10.000</MENGE>'
        "<MENEE>PC</MENEE><NETWR>%s</NETWR><VGBEL>%s</VGBEL>"
        "<VGPOS>%s</VGPOS>%s</E1EDP01>"
        '<E1EDS01 SEGMENT="1"><SUMID>010</SUMID><SUMME>%s</SUMME></E1EDS01>'
        '<E1EDS01 SEGMENT="1"><SUMID>011</SUMID><SUMME>%s</SUMME></E1EDS01>'
        '<E1EDS01 SEGMENT="1"><SUMID>205</SUMID><SUMME>%s</SUMME></E1EDS01>'
        "</IDOC></INVOIC02>"
    ) % (reference, reference, date, supplier, supplier, net,
         purchase_order, position, quoted, gross, net, tax)


class GoodsReceiptCase(MockServerCase):
    def post_json(self, path, payload):
        headers = dict(self.csrf_token(),
                       **{"Content-Type": "application/json",
                          "Accept": "application/json"})
        return self.request("POST", path, body=json.dumps(payload),
                            headers=headers)

    def receive(self, delivery_note="DN-0001", purchase_order="4500000100",
                position="00010", quantity="10.000", **kw):
        """Post a goods receipt and return its document and year."""
        payload = {"ReferenceDocument": delivery_note,
                   "PostingDate": "2026-09-26T00:00:00",
                   "to_MaterialDocumentItem": [{
                       "PurchaseOrder": purchase_order,
                       "PurchaseOrderItem": position,
                       "Material": "M-1", "Plant": "1710",
                       "QuantityInEntryUnit": quantity, "EntryUnit": "PC"}]}
        payload.update(kw)
        status, _, body = self.post_json(MD + "/A_MaterialDocumentHeader",
                                         payload)
        self.assertEqual(status, 201, body)
        return body["d"]["MaterialDocument"], body["d"]["MaterialDocumentYear"]

    def send(self, **kw):
        headers = dict(self.csrf_token(),
                       **{"Content-Type": "application/xml",
                          "Accept": "application/json"})
        status, _, receipt = self.request("POST", "/sap/bc/idoc",
                                          body=invoic(**kw), headers=headers)
        self.assertEqual(status, 201)
        return receipt

    def items_of(self, reference):
        _, _, body = self.get(
            SRV + "/A_SuplrInvcItemPurOrdRef?$format=json&$filter="
            "SupplierInvoice eq '%s'" % reference)
        return body["d"]["results"]

    def counts(self):
        """How many supplier invoices and open payables exist right now."""
        _, _, invoices = self.get(SRV + "/A_SupplierInvoice?$format=json")
        _, _, items = self.get(
            CUBE + "?$format=json&$filter=AccountingDocumentItemType eq 'K'")
        return (len(invoices["d"]["results"]), len(items["d"]["results"]))

    def invoice_numbered(self, reference):
        _, _, body = self.get(
            SRV + "/A_SupplierInvoice?$format=json&$filter="
            "SupplierInvoiceIDByInvcgParty eq '%s'" % reference)
        return body["d"]["results"]


class TestPostingAGoodsReceipt(GoodsReceiptCase):
    """What a receipt is, and what it refuses."""

    def test_a_receipt_is_readable_and_keeps_its_delivery_note(self):
        document, year = self.receive(delivery_note="DN-READ")
        _, _, body = self.get(
            "%s/A_MaterialDocumentHeader(MaterialDocument='%s',"
            "MaterialDocumentYear='%s')?$format=json" % (MD, document, year))
        self.assertEqual(body["d"]["ReferenceDocument"], "DN-READ")

    def test_the_number_comes_from_the_material_document_range(self):
        first, _ = self.receive(delivery_note="DN-A")
        second, _ = self.receive(delivery_note="DN-B")
        self.assertTrue(first.startswith("49"), first)
        self.assertEqual(int(second), int(first) + 1)
        self.assertEqual(len(first), 10)

    def test_its_items_are_numbered_and_say_they_are_a_receipt(self):
        document, year = self.receive(delivery_note="DN-ITEMS")
        _, _, body = self.get(
            "%s/A_MaterialDocumentItem?$format=json&$filter="
            "MaterialDocument eq '%s'" % (MD, document))
        [item] = body["d"]["results"]
        self.assertEqual(item["MaterialDocumentItem"], "0001")
        self.assertEqual(item["GoodsMovementType"], "101")
        self.assertEqual(item["PurchaseOrderItem"], "00010")

    def test_a_receipt_with_no_items_receives_nothing_and_is_refused(self):
        status, _, body = self.post_json(
            MD + "/A_MaterialDocumentHeader",
            {"ReferenceDocument": "DN-EMPTY",
             "to_MaterialDocumentItem": []})
        self.assertEqual(status, 400, body)
        self.assertIn("receives nothing", json.dumps(body))

    def test_an_item_naming_no_purchase_order_is_refused(self):
        status, _, body = self.post_json(
            MD + "/A_MaterialDocumentHeader",
            {"ReferenceDocument": "DN-NOPO",
             "to_MaterialDocumentItem": [{"Material": "M-1",
                                          "QuantityInEntryUnit": "1.000"}]})
        self.assertEqual(status, 400, body)
        self.assertIn("purchase order", json.dumps(body))

    def test_nothing_is_stored_by_a_refused_receipt(self):
        _, _, before = self.get(
            MD + "/A_MaterialDocumentHeader/$count", )
        self.post_json(MD + "/A_MaterialDocumentHeader",
                       {"ReferenceDocument": "DN-GONE",
                        "to_MaterialDocumentItem": []})
        _, _, body = self.get(
            "%s/A_MaterialDocumentHeader?$format=json&$filter="
            "ReferenceDocument eq 'DN-GONE'" % MD)
        self.assertEqual(body["d"]["results"], [])

    def test_a_client_cannot_choose_the_document_number(self):
        status, _, body = self.post_json(
            MD + "/A_MaterialDocumentHeader",
            {"MaterialDocument": "4900009999",
             "ReferenceDocument": "DN-MINE",
             "to_MaterialDocumentItem": [{"PurchaseOrder": "4500000100",
                                          "QuantityInEntryUnit": "1.000"}]})
        self.assertEqual(status, 400, body)
        self.assertIn("MaterialDocument", json.dumps(body))


class TestAnInvoiceThatQuotesAReceipt(GoodsReceiptCase):
    """`E1EDP02` `QUALF` `016`, resolved or refused."""

    def test_the_item_carries_the_receipt_it_billed(self):
        document, year = self.receive(delivery_note="DN-BILLED")
        self.send(reference="INV-GR-1", delivery_note="DN-BILLED")
        [item] = self.items_of(self.invoice_numbered("INV-GR-1")[0]
                               ["SupplierInvoice"])
        self.assertEqual(item["ReferenceDocument"], document)
        self.assertEqual(item["ReferenceDocumentFiscalYear"], year)
        self.assertEqual(item["ReferenceDocumentItem"], "0001")

    def test_an_invoice_quoting_no_delivery_note_leaves_it_blank(self):
        self.send(reference="INV-GR-2")
        [item] = self.items_of(self.invoice_numbered("INV-GR-2")[0]
                               ["SupplierInvoice"])
        self.assertEqual(item["ReferenceDocument"], "")
        self.assertEqual(item["ReferenceDocumentFiscalYear"], "")
        self.assertEqual(item["ReferenceDocumentItem"], "")

    def test_a_delivery_note_no_receipt_carries_is_status_51(self):
        receipt = self.send(reference="INV-GR-3", delivery_note="DN-NOBODY")
        # A refused INVOIC has no APPLIED list: there is nothing it was
        # applied to, and the status is the receipt's own.
        self.assertEqual(receipt["STATUS"], "51")
        self.assertNotIn("APPLIED", receipt)
        self.assertIn("DN-NOBODY", receipt["STATUS_TEXT"])
        self.assertIn("016", receipt["STATUS_TEXT"])
        self.assertIn("4500000100", receipt["STATUS_TEXT"])

    def test_and_posts_nothing_at_all(self):
        """Not merely no invoice: no payable either. A refusal that left an
        open item behind would be owed to somebody."""
        invoices, payables = self.counts()
        self.send(reference="INV-GR-4", delivery_note="DN-NOBODY")
        self.assertEqual(self.invoice_numbered("INV-GR-4"), [])
        self.assertEqual(self.counts(), (invoices, payables))

    def test_a_receipt_that_received_nothing_against_this_line_is_refused(self):
        """The note finds a receipt; the receipt has no item for this
        purchase order line. Answering with the header alone would make the
        item number a guess."""
        self.receive(delivery_note="DN-OTHERLINE", position="00010")
        receipt = self.send(reference="INV-GR-5",
                            delivery_note="DN-OTHERLINE", position="00090")
        self.assertEqual(receipt["STATUS"], "51")
        self.assertIn("00090", receipt["STATUS_TEXT"])
        self.assertIn("DN-OTHERLINE", receipt["STATUS_TEXT"])

    def test_leading_zeros_on_the_position_do_not_matter(self):
        document, _ = self.receive(delivery_note="DN-ZEROS", position="00010")
        self.send(reference="INV-GR-6", delivery_note="DN-ZEROS",
                  position="10")
        [item] = self.items_of(self.invoice_numbered("INV-GR-6")[0]
                               ["SupplierInvoice"])
        self.assertEqual(item["ReferenceDocument"], document)

    def test_one_delivery_note_against_two_orders_resolves_to_the_right_one(self):
        """A delivery note is the supplier's own number for a shipment, so it
        means nothing across purchase orders - the same lesson #87 taught
        about invoice numbers across suppliers. Two receipts under one note
        must not be told apart by the note alone.
        """
        first, _ = self.receive(delivery_note="DN-SHARED",
                                purchase_order="4500000100", position="00010")
        second, _ = self.receive(delivery_note="DN-SHARED",
                                 purchase_order="4500000101", position="00010")
        self.assertNotEqual(first, second)
        self.send(reference="INV-GR-10", delivery_note="DN-SHARED",
                  purchase_order="4500000101")
        [item] = self.items_of(self.invoice_numbered("INV-GR-10")[0]
                               ["SupplierInvoice"])
        self.assertEqual(item["ReferenceDocument"], second)
        self.assertEqual(item["PurchaseOrder"], "4500000101")

    def test_what_was_billed_against_one_receipt_is_filterable(self):
        document, _ = self.receive(delivery_note="DN-SUM")
        other, _ = self.receive(delivery_note="DN-SUM-OTHER")
        self.send(reference="INV-GR-7", delivery_note="DN-SUM")
        self.send(reference="INV-GR-8", delivery_note="DN-SUM")
        self.send(reference="INV-GR-9", delivery_note="DN-SUM-OTHER")
        _, _, body = self.get(
            SRV + "/A_SuplrInvcItemPurOrdRef?$format=json&$filter="
            "ReferenceDocument eq '%s'" % document)
        quantities = [row["QuantityInPurchaseOrderUnit"]
                      for row in body["d"]["results"]]
        self.assertEqual(len(quantities), 2, body["d"]["results"])
        self.assertEqual(set(quantities), {"10.000"})
        _, _, only = self.get(
            SRV + "/A_SuplrInvcItemPurOrdRef?$format=json&$filter="
            "ReferenceDocument eq '%s'" % other)
        self.assertEqual(len(only["d"]["results"]), 1)


if __name__ == "__main__":
    unittest.main()
