"""The documents a sales order turns into: delivery, invoice, journal entry."""
import unittest

from support import MockServerCase, SRV

DELIVERIES = "/sap/opu/odata/sap/API_OUTBOUND_DELIVERY_SRV"
BILLING = "/sap/opu/odata/sap/API_BILLING_DOCUMENT_SRV"
JOURNAL = "/sap/opu/odata/sap/API_JOURNALENTRY_SRV"


class DocumentCase(MockServerCase):
    def call(self, name, params):
        return self.request("POST", "/sap/bc/rfc/" + name, body=params,
                            headers=self.csrf_token())

    def an_order_with_items(self):
        _, _, body = self.get(
            SRV + "/A_SalesOrder?$top=5&$expand=to_Item&$format=json")
        for row in body["d"]["results"]:
            if row["to_Item"]["results"]:
                return row
        self.fail("the seed should contain an order with items")

    def fresh_order(self, quantity="4"):
        _, _, created = self.request("POST", SRV + "/A_SalesOrder?$expand=to_Item",
                                     headers=self.csrf_token(), body={
            "SalesOrderType": "OR", "SalesOrganization": "1710", "SoldToParty": "1000001",
            "DistributionChannel": "10", "OrganizationDivision": "00",
            "TransactionCurrency": "EUR",
            "to_Item": [{"Material": "TG11", "RequestedQuantity": quantity,
                         "RequestedQuantityUnit": "PC", "NetAmount": "400.00"}]})
        return created["d"]


class TestSeededChain(DocumentCase):
    def test_the_documents_reference_what_they_came_from(self):
        _, _, deliveries = self.get(
            DELIVERIES + "/A_OutbDeliveryHeader?$top=1&$expand=to_DeliveryDocumentItem"
            "&$format=json")
        delivery = deliveries["d"]["results"][0]
        items = delivery["to_DeliveryDocumentItem"]["results"]
        self.assertTrue(items)

        order_number = items[0]["ReferenceSDDocument"]
        status, _, order = self.get(SRV + "/A_SalesOrder('%s')?$expand=to_Item&$format=json"
                                    % order_number)
        self.assertEqual(status, 200, "a delivery item names an order that exists")
        positions = {row["SalesOrderItem"] for row in order["d"]["to_Item"]["results"]}
        for item in items:
            self.assertIn(item["ReferenceSDDocumentItem"], positions)

    def test_an_invoice_adds_up_and_points_at_its_journal_entry(self):
        _, _, invoices = self.get(
            BILLING + "/A_BillingDocument?$top=3&$expand=to_Item&$format=json")
        self.assertTrue(invoices["d"]["results"])
        for invoice in invoices["d"]["results"]:
            net = sum(float(item["NetAmount"]) for item in invoice["to_Item"]["results"])
            self.assertAlmostEqual(float(invoice["TotalNetAmount"]), net, places=2)
            self.assertAlmostEqual(
                float(invoice["TotalGrossAmount"]),
                float(invoice["TotalNetAmount"]) + float(invoice["TotalTaxAmount"]),
                places=2)

            status, _, entry = self.get(
                JOURNAL + "/A_JournalEntry(AccountingDocument='%s',CompanyCode='1710',"
                "FiscalYear='%s')?$expand=to_JournalEntryItem&$format=json"
                % (invoice["AccountingDocument"],
                   invoice["BillingDocumentDate"] and
                   self.year_of(invoice["BillingDocumentDate"])))
            self.assertEqual(status, 200, "the invoice names a journal entry that exists")
            self.assertTrue(entry["d"]["to_JournalEntryItem"]["results"])

    def test_journal_entries_balance(self):
        _, _, entries = self.get(
            JOURNAL + "/A_JournalEntry?$top=4&$expand=to_JournalEntryItem&$format=json")
        self.assertTrue(entries["d"]["results"])
        for entry in entries["d"]["results"]:
            debits = sum(float(line["AmountInTransactionCurrency"])
                         for line in entry["to_JournalEntryItem"]["results"]
                         if line["DebitCreditCode"] == "S")
            credits = sum(float(line["AmountInTransactionCurrency"])
                          for line in entry["to_JournalEntryItem"]["results"]
                          if line["DebitCreditCode"] == "H")
            self.assertAlmostEqual(debits, credits, places=2,
                                   msg=entry["AccountingDocument"])

    def year_of(self, odata_date):
        import datetime
        milliseconds = int(odata_date.strip("/").replace("Date(", "").replace(")", ""))
        return str((datetime.datetime(1970, 1, 1)
                    + datetime.timedelta(milliseconds=milliseconds)).year)


class TestDeliveryCreation(DocumentCase):
    def test_a_delivery_is_created_and_readable(self):
        order = self.fresh_order()
        item = order["to_Item"]["results"][0]

        status, _, body = self.call("BAPI_OUTB_DELIVERY_CREATE_SLS", {
            "SALES_ORDER_ITEMS": [{"REF_DOC": order["SalesOrder"],
                                   "REF_ITEM": item["SalesOrderItem"],
                                   "DLV_QTY": item["RequestedQuantity"]}]})
        self.assertEqual(status, 200)
        self.assertEqual(body["RETURN"][0]["TYPE"], "S")
        delivery = body["DELIVERY"]
        self.assertRegex(delivery, r"^\d{10}$")

        status, _, entity = self.get(
            DELIVERIES + "/A_OutbDeliveryHeader('%s')?$expand=to_DeliveryDocumentItem"
            "&$format=json" % delivery)
        self.assertEqual(status, 200, "the number the BAPI returned addresses a document")
        self.assertEqual(entity["d"]["SoldToParty"], order["SoldToParty"])
        lines = entity["d"]["to_DeliveryDocumentItem"]["results"]
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["ReferenceSDDocument"], order["SalesOrder"])
        self.assertEqual(lines[0]["Material"], item["Material"])

        _, _, after = self.get(SRV + "/A_SalesOrder('%s')?$format=json" % order["SalesOrder"])
        self.assertEqual(after["d"]["OverallDeliveryStatus"], "C")

    def test_a_partial_delivery_leaves_the_order_partly_delivered(self):
        order = self.fresh_order(quantity="10")
        item = order["to_Item"]["results"][0]
        _, _, body = self.call("BAPI_OUTB_DELIVERY_CREATE_SLS", {
            "SALES_ORDER_ITEMS": [{"REF_DOC": order["SalesOrder"],
                                   "REF_ITEM": item["SalesOrderItem"], "DLV_QTY": "3"}]})
        self.assertEqual(body["RETURN"][0]["TYPE"], "S")
        _, _, after = self.get(SRV + "/A_SalesOrder('%s')?$format=json" % order["SalesOrder"])
        self.assertEqual(after["d"]["OverallDeliveryStatus"], "B")

    def test_the_refusals(self):
        order = self.fresh_order()
        item = order["to_Item"]["results"][0]

        _, _, body = self.call("BAPI_OUTB_DELIVERY_CREATE_SLS", {})
        self.assertEqual(body["RETURN"][0]["TYPE"], "E")

        _, _, body = self.call("BAPI_OUTB_DELIVERY_CREATE_SLS", {
            "SALES_ORDER_ITEMS": [{"REF_DOC": "9999999999", "REF_ITEM": "000010",
                                   "DLV_QTY": "1"}]})
        self.assertEqual(body["RETURN"][0]["TYPE"], "E")
        self.assertIn("9999999999", body["RETURN"][0]["MESSAGE"])

        _, _, body = self.call("BAPI_OUTB_DELIVERY_CREATE_SLS", {
            "SALES_ORDER_ITEMS": [{"REF_DOC": order["SalesOrder"], "REF_ITEM": "009999",
                                   "DLV_QTY": "1"}]})
        self.assertEqual(body["RETURN"][0]["TYPE"], "E")

        _, _, body = self.call("BAPI_OUTB_DELIVERY_CREATE_SLS", {
            "SALES_ORDER_ITEMS": [{"REF_DOC": order["SalesOrder"],
                                   "REF_ITEM": item["SalesOrderItem"],
                                   "DLV_QTY": "99999"}]})
        self.assertEqual(body["RETURN"][0]["TYPE"], "E")
        self.assertIn("open", body["RETURN"][0]["MESSAGE"])
        self.assertEqual(body["DELIVERY"], "", "nothing is created when it is refused")


class TestAccountingPost(DocumentCase):
    def balanced_posting(self, amount="1000.00"):
        return {
            "DOCUMENTHEADER": {"COMP_CODE": "1710", "DOC_TYPE": "SA",
                               "PSTNG_DATE": "2026-09-11", "HEADER_TXT": "Manual posting",
                               "REF_DOC_NO": "TEST-1"},
            "ACCOUNTGL": [
                {"ITEMNO_ACC": "1", "GL_ACCOUNT": "0012100000", "ITEM_TEXT": "Receivable",
                 "CUSTOMER": "1000001"},
                {"ITEMNO_ACC": "2", "GL_ACCOUNT": "0041000000", "ITEM_TEXT": "Revenue",
                 "PROFIT_CTR": "YB110"}],
            "CURRENCYAMOUNT": [
                {"ITEMNO_ACC": "1", "CURRENCY": "EUR", "AMT_DOCCUR": amount},
                {"ITEMNO_ACC": "2", "CURRENCY": "EUR", "AMT_DOCCUR": "-" + amount}],
        }

    def test_a_posting_becomes_a_readable_journal_entry(self):
        status, _, body = self.call("BAPI_ACC_DOCUMENT_POST", self.balanced_posting())
        self.assertEqual(status, 200)
        self.assertEqual(body["RETURN"][0]["TYPE"], "S")
        self.assertEqual(body["OBJ_TYPE"], "BKPFF")

        key = body["OBJ_KEY"]
        self.assertEqual(len(key), 18, "document, company code and fiscal year")
        document, company, year = key[:10], key[10:14], key[14:]
        self.assertEqual(company, "1710")

        status, _, entry = self.get(
            JOURNAL + "/A_JournalEntry(AccountingDocument='%s',CompanyCode='%s',"
            "FiscalYear='%s')?$expand=to_JournalEntryItem&$format=json"
            % (document, company, year))
        self.assertEqual(status, 200)
        self.assertEqual(entry["d"]["AccountingDocumentHeaderText"], "Manual posting")
        lines = entry["d"]["to_JournalEntryItem"]["results"]
        self.assertEqual(len(lines), 2)
        sides = {line["DebitCreditCode"]: line for line in lines}
        self.assertEqual(set(sides), {"S", "H"},
                         "a positive amount debits, a negative one credits")
        self.assertEqual(sides["S"]["Customer"], "1000001")
        self.assertEqual(float(sides["S"]["AmountInTransactionCurrency"]),
                         float(sides["H"]["AmountInTransactionCurrency"]))

    def test_an_unbalanced_document_is_refused(self):
        posting = self.balanced_posting()
        posting["CURRENCYAMOUNT"][1]["AMT_DOCCUR"] = "-900.00"
        _, _, body = self.call("BAPI_ACC_DOCUMENT_POST", posting)
        self.assertEqual(body["RETURN"][0]["TYPE"], "E")
        self.assertIn("Balance", body["RETURN"][0]["MESSAGE"])
        self.assertEqual(body["OBJ_KEY"], "")

    def test_the_other_refusals(self):
        posting = self.balanced_posting()
        del posting["DOCUMENTHEADER"]["COMP_CODE"]
        _, _, body = self.call("BAPI_ACC_DOCUMENT_POST", posting)
        self.assertEqual(body["RETURN"][0]["TYPE"], "E")

        posting = self.balanced_posting()
        posting["ACCOUNTGL"] = []
        _, _, body = self.call("BAPI_ACC_DOCUMENT_POST", posting)
        self.assertEqual(body["RETURN"][0]["TYPE"], "E")

        posting = self.balanced_posting()
        posting["CURRENCYAMOUNT"] = [posting["CURRENCYAMOUNT"][0]]
        _, _, body = self.call("BAPI_ACC_DOCUMENT_POST", posting)
        self.assertEqual(body["RETURN"][0]["TYPE"], "E")


class TestIdocsLeaveDocumentsBehind(DocumentCase):
    def test_invoic02_creates_a_billing_document_and_posts_it(self):
        order = self.an_order_with_items()
        status, _, generated = self.request(
            "POST", "/sap/bc/idoc/generate",
            body={"mestyp": "INVOIC", "SalesOrder": order["SalesOrder"]},
            headers=self.csrf_token())
        self.assertEqual(status, 201)

        status, _, invoice = self.get(
            BILLING + "/A_BillingDocument('%s')?$expand=to_Item&$format=json"
            % generated["billing_document"])
        self.assertEqual(status, 200, "the IDoc describes a document that exists")
        self.assertEqual(invoice["d"]["AccountingDocument"],
                         generated["accounting_document"])
        self.assertEqual(invoice["d"]["SoldToParty"], order["SoldToParty"])
        self.assertEqual(len(invoice["d"]["to_Item"]["results"]),
                         len(order["to_Item"]["results"]))

    def test_delvry07_creates_a_delivery(self):
        order = self.fresh_order()
        status, _, generated = self.request(
            "POST", "/sap/bc/idoc/generate",
            body={"mestyp": "DELVRY", "SalesOrder": order["SalesOrder"]},
            headers=self.csrf_token())
        self.assertEqual(status, 201)

        status, _, delivery = self.get(
            DELIVERIES + "/A_OutbDeliveryHeader('%s')?$format=json" % generated["delivery"])
        self.assertEqual(status, 200)
        self.assertEqual(delivery["d"]["SoldToParty"], order["SoldToParty"])
        self.assertIn(generated["delivery"], generated["xml"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
