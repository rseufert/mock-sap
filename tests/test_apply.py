"""$apply: aggregation pipelines on the V4 services."""
import unittest
from urllib.parse import quote

from support import MockServerCase, SRV

V4 = "/sap/opu/odata4/sap/api_salesorder/srvd_a2x/sap/api_salesorder/0001"


class ApplyCase(MockServerCase):
    def apply(self, pipeline, extra=""):
        url = "%s/SalesOrder?$apply=%s%s" % (V4, quote(pipeline, safe="(),/$"), extra)
        return self.get(url)

    def all_orders(self):
        _, _, body = self.get(V4 + "/SalesOrder?$top=1000")
        return body["value"]


class TestAggregation(ApplyCase):
    def test_aggregate_over_everything(self):
        status, headers, body = self.apply(
            "aggregate(TotalNetAmount with sum as Total,"
            "TotalNetAmount with average as Avg,"
            "TotalNetAmount with min as Low,TotalNetAmount with max as High,"
            "$count as Orders)")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("OData-Version"), "4.0")
        self.assertEqual(len(body["value"]), 1)
        row = body["value"][0]

        orders = self.all_orders()
        amounts = [o["TotalNetAmount"] for o in orders]
        self.assertEqual(row["Orders"], len(orders))
        self.assertAlmostEqual(row["Total"], sum(amounts), places=2)
        self.assertAlmostEqual(row["Avg"], sum(amounts) / len(amounts), places=2)
        self.assertAlmostEqual(row["Low"], min(amounts), places=2)
        self.assertAlmostEqual(row["High"], max(amounts), places=2)

    def test_groupby_produces_rows_that_are_not_entities(self):
        status, _, body = self.apply(
            "groupby((SalesOrganization),aggregate(TotalNetAmount with sum as Total))")
        self.assertEqual(status, 200)
        row = body["value"][0]
        self.assertEqual(set(row), {"SalesOrganization", "Total"},
                         "only what the pipeline produced")
        self.assertNotIn("@odata.etag", row)
        self.assertNotIn("SalesOrder", row)
        self.assertIn("#SalesOrder(SalesOrganization,Total)", body["@odata.context"])

    def test_grouping_on_several_properties(self):
        _, _, body = self.apply(
            "groupby((SalesOrganization,OverallSDProcessStatus),aggregate($count as N))")
        keys = [(r["SalesOrganization"], r["OverallSDProcessStatus"])
                for r in body["value"]]
        self.assertEqual(len(keys), len(set(keys)), "one row per distinct combination")
        self.assertEqual(sum(r["N"] for r in body["value"]), len(self.all_orders()))

    def test_filter_then_group(self):
        _, _, body = self.apply(
            "filter(OverallSDProcessStatus eq 'A')/groupby((SoldToParty),"
            "aggregate($count as Orders))")
        expected = len([o for o in self.all_orders()
                        if o["OverallSDProcessStatus"] == "A"])
        self.assertEqual(sum(r["Orders"] for r in body["value"]), expected)

    def test_countdistinct(self):
        _, _, body = self.apply("aggregate(SoldToParty with countdistinct as Customers)")
        distinct = {o["SoldToParty"] for o in self.all_orders()}
        self.assertEqual(body["value"][0]["Customers"], len(distinct))

    def test_ordering_and_paging_inside_the_pipeline(self):
        _, _, body = self.apply(
            "groupby((SoldToParty),aggregate($count as Orders))"
            "/orderby(Orders desc,SoldToParty asc)/top(3)")
        counts = [r["Orders"] for r in body["value"]]
        self.assertEqual(len(counts), 3)
        self.assertEqual(counts, sorted(counts, reverse=True))

        _, _, skipped = self.apply(
            "groupby((SoldToParty),aggregate($count as Orders))"
            "/orderby(SoldToParty asc)/skip(1)/top(1)")
        _, _, first = self.apply(
            "groupby((SoldToParty),aggregate($count as Orders))"
            "/orderby(SoldToParty asc)/top(1)")
        self.assertNotEqual(skipped["value"][0]["SoldToParty"],
                            first["value"][0]["SoldToParty"])

    def test_outer_options_apply_after_the_pipeline(self):
        _, _, body = self.apply(
            "groupby((SoldToParty),aggregate($count as Orders))",
            extra="&$orderby=" + quote("Orders desc") + "&$top=2")
        self.assertEqual(len(body["value"]), 2)
        self.assertGreaterEqual(body["value"][0]["Orders"], body["value"][1]["Orders"])

    def test_count_counts_groups_not_the_page(self):
        _, _, all_groups = self.apply("groupby((SoldToParty),aggregate($count as N))")
        _, _, page = self.apply(
            "groupby((SoldToParty),aggregate($count as N))/top(2)", extra="&$count=true")
        self.assertEqual(len(page["value"]), 2)
        self.assertEqual(page["@odata.count"], len(all_groups["value"]))

    def test_decimal_aggregates_keep_their_scale(self):
        _, _, body = self.apply("aggregate(TotalNetAmount with average as Avg)")
        average = body["value"][0]["Avg"]
        self.assertEqual(round(average, 3), average,
                         "an average of amounts is an amount, not a repeating float")


class TestApplyRefusals(ApplyCase):
    def test_unknown_transformation_names_what_is_supported(self):
        status, _, body = self.apply("nonsense((X))")
        self.assertEqual(status, 400)
        for known in ("filter", "groupby", "aggregate"):
            self.assertIn(known, body["error"]["message"])

    def test_unknown_properties_and_methods(self):
        for pipeline in ("groupby((Nope),aggregate($count as N))",
                         "aggregate(Nope with sum as N)",
                         "aggregate(TotalNetAmount with median as N)",
                         "aggregate(SalesOrderType with sum as N)"):
            status, _, body = self.apply(pipeline)
            self.assertEqual(status, 400, pipeline)
            self.assertIn("error", body)

    def test_malformed_pipelines(self):
        for pipeline in ("groupby(SalesOrganization)", "aggregate(TotalNetAmount)",
                         "filter(TotalNetAmount gt 1)", "top(2)", "groupby(())"):
            status, _, _ = self.apply(pipeline)
            self.assertEqual(status, 400, pipeline)

    def test_orderby_must_name_what_the_pipeline_produced(self):
        status, _, body = self.apply("groupby((SoldToParty))/orderby(TotalNetAmount)")
        self.assertEqual(status, 400)
        self.assertIn("SoldToParty", body["error"]["message"])

    def test_options_that_cannot_be_combined(self):
        for extra in ("&$select=SalesOrder", "&$expand=to_Item",
                      "&$filter=" + quote("TotalNetAmount gt 1")):
            status, _, body = self.apply(
                "groupby((SoldToParty),aggregate($count as N))", extra=extra)
            self.assertEqual(status, 400, extra)
            self.assertIn("$apply", body["error"]["message"])

    def test_count_segment_is_refused(self):
        status, _, body = self.get(
            V4 + "/SalesOrder/$count?$apply=" + quote("aggregate($count as N)"))
        self.assertEqual(status, 400)
        self.assertIn("$count=true", body["error"]["message"])

    def test_v2_services_do_not_aggregate(self):
        status, _, body = self.get(
            SRV + "/A_SalesOrder?$apply=" + quote("aggregate($count as N)") + "&$format=json")
        self.assertEqual(status, 400)
        self.assertIn("V4", body["error"]["message"]["value"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
