"""Amounts are decimals: the scale survives, and a half rounds up.

Every assertion here computes the number it expects with `decimal.Decimal`
rather than asking the mock twice, because that is the whole claim - that
what comes back is the arithmetic a packed decimal would do, not what a
binary float happened to land on.
"""
from __future__ import annotations

import unittest
from decimal import ROUND_HALF_UP, Decimal
from urllib.parse import quote

from support import MockServerCase, SRV

BILLING = "/sap/opu/odata/sap/API_BILLING_DOCUMENT_SRV"
V4 = "/sap/opu/odata4/sap/api_salesorder/srvd_a2x/sap/api_salesorder/0001"

CENT = Decimal("0.01")
TAX_RATE = Decimal("0.19")


class AmountCase(MockServerCase):
    def order_of(self, *amounts, **kw):
        """An order whose items net `amounts`, as the strings a client sends."""
        items = [{"Material": "TG11", "RequestedQuantity": "1",
                  "RequestedQuantityUnit": "PC", "NetAmount": amount}
                 for amount in amounts]
        status, _, created = self.request(
            "POST", SRV + "/A_SalesOrder?$expand=to_Item",
            headers=self.csrf_token(), body=dict({
                "SalesOrderType": "OR", "SalesOrganization": "1710",
                "SoldToParty": "1000001", "DistributionChannel": "10",
                "OrganizationDivision": "00", "TransactionCurrency": "EUR",
                "to_Item": items}, **kw))
        self.assertEqual(status, 201)
        return created["d"]

    def bill(self, order):
        """Invoice an order the way a client can: generate its INVOIC02."""
        status, _, generated = self.request(
            "POST", "/sap/bc/idoc/generate",
            body={"mestyp": "INVOIC", "SalesOrder": order["SalesOrder"]},
            headers=self.csrf_token())
        self.assertEqual(status, 201)
        status, _, invoice = self.get(
            BILLING + "/A_BillingDocument('%s')?$expand=to_Item&$format=json"
            % generated["billing_document"])
        self.assertEqual(status, 200)
        return invoice["d"]


class TestRounding(AmountCase):
    def test_tax_on_an_exact_half_cent_rounds_up(self):
        """19% of 2.50 is 0.4750, and a tax authority rounds that to 0.48.

        The float answer was 0.47, twice over: 2.50 * 0.19 lands just below
        0.475 in binary, and `round` takes a half to the nearer even digit.
        """
        invoice = self.bill(self.order_of("2.50"))
        net = Decimal(invoice["TotalNetAmount"])
        self.assertEqual(net, Decimal("2.50"))
        self.assertEqual(
            Decimal(invoice["TotalTaxAmount"]),
            (net * TAX_RATE).quantize(CENT, rounding=ROUND_HALF_UP))
        self.assertEqual(Decimal(invoice["TotalTaxAmount"]), Decimal("0.48"))

    def test_an_amount_is_stored_at_the_scale_its_property_declares(self):
        """`NetAmount` is Decimal(16,3), so 2.4995 is 2.500 and not 2.499.

        Printing the float to three places gave 2.499, because the double
        nearest 2.4995 is below it - the one digit a client would check.
        """
        order = self.order_of("2.5", "2.4995")
        plain, half = order["to_Item"]["results"]
        self.assertEqual(plain["NetAmount"], "2.500")
        self.assertEqual(half["NetAmount"], "2.500")

    def test_a_gross_amount_is_its_net_plus_its_tax(self):
        invoice = self.bill(self.order_of("19.99", "0.03", "7.77"))
        self.assertEqual(
            Decimal(invoice["TotalGrossAmount"]),
            Decimal(invoice["TotalNetAmount"]) + Decimal(invoice["TotalTaxAmount"]))
        for item in invoice["to_Item"]["results"]:
            self.assertEqual(
                Decimal(item["TaxAmount"]),
                (Decimal(item["NetAmount"]) * TAX_RATE).quantize(
                    CENT, rounding=ROUND_HALF_UP))

    def test_an_order_total_is_exactly_the_sum_of_its_items(self):
        order = self.order_of("0.01", "0.02", "0.04", "0.08")
        _, _, body = self.get(
            SRV + "/A_SalesOrder('%s')?$expand=to_Item&$format=json"
            % order["SalesOrder"])
        items = body["d"]["to_Item"]["results"]
        self.assertEqual(
            Decimal(body["d"]["TotalNetAmount"]),
            sum((Decimal(item["NetAmount"]) for item in items), Decimal(0)))


class TestRefusals(AmountCase):
    def test_a_value_that_is_not_an_amount_is_refused(self):
        """Including the ones a float accepted: nan, inf, and 1e40.

        `float('nan')` was stored and served back as `nan`, which is not a
        number any client can read, and 1e40 does not fit the Decimal(16,3)
        the property declares.
        """
        for value in ("", "   ", "abc", "nan", "inf", "-Infinity", "1e40"):
            status, _, body = self.request(
                "POST", SRV + "/A_SalesOrder?$expand=to_Item",
                headers=self.csrf_token(), body={
                    "SalesOrderType": "OR", "SalesOrganization": "1710",
                    "SoldToParty": "1000001", "DistributionChannel": "10",
                    "OrganizationDivision": "00", "TransactionCurrency": "EUR",
                    "to_Item": [{"Material": "TG11", "RequestedQuantity": "1",
                                 "RequestedQuantityUnit": "PC",
                                 "NetAmount": value}]})
            self.assertEqual(status, 400, "NetAmount %r" % value)
            self.assertIn("NetAmount", body["error"]["message"]["value"])


class TestQuerying(AmountCase):
    """The column is text now, and SQLite would compare it as text."""

    def amounts(self, query):
        _, _, body = self.get(SRV + "/A_SalesOrder?" + query + "&$format=json")
        return [Decimal(row["TotalNetAmount"]) for row in body["d"]["results"]]

    def test_amounts_sort_as_numbers_and_not_as_strings(self):
        for amount in ("9.00", "99.00", "100.00", "1190.00"):
            self.order_of(amount, PurchaseOrderByCustomer="SORT")
        rising = self.amounts(
            "$filter=" + quote("PurchaseOrderByCustomer eq 'SORT'")
            + "&$orderby=TotalNetAmount asc")
        self.assertEqual(rising, sorted(rising))
        self.assertEqual(rising[0], Decimal("9.00"))
        self.assertEqual(rising[-1], Decimal("1190.00"))

    def test_a_filter_on_an_amount_compares_numbers(self):
        self.order_of("9.00", PurchaseOrderByCustomer="FILTER")
        self.order_of("1190.00", PurchaseOrderByCustomer="FILTER")
        kept = self.amounts(
            "$filter=" + quote("PurchaseOrderByCustomer eq 'FILTER' "
                               "and TotalNetAmount gt 100"))
        self.assertEqual(kept, [Decimal("1190.00")])

    def test_an_aggregate_over_amounts_is_numeric(self):
        _, _, body = self.get(
            "%s/SalesOrder?$apply=%s" % (V4, quote(
                "aggregate(TotalNetAmount with max as High,"
                "TotalNetAmount with min as Low)", safe="(),/$")))
        _, _, all_orders = self.get(V4 + "/SalesOrder?$top=1000")
        amounts = [Decimal(str(row["TotalNetAmount"]))
                   for row in all_orders["value"]]
        self.assertEqual(Decimal(str(body["value"][0]["High"])), max(amounts))
        self.assertEqual(Decimal(str(body["value"][0]["Low"])), min(amounts))


if __name__ == "__main__":
    unittest.main(verbosity=2)
