"""ETags and optimistic concurrency: If-Match, If-None-Match and 412."""
import unittest

from support import MockServerCase, SRV


class ETagCase(MockServerCase):
    def an_order(self):
        _, _, body = self.get(SRV + "/A_SalesOrder?$top=1&$format=json")
        return body["d"]["results"][0]["SalesOrder"]

    def read(self, order):
        """Return (etag, entity) for one sales order."""
        status, headers, body = self.get(SRV + "/A_SalesOrder('%s')?$format=json" % order)
        self.assertEqual(status, 200)
        return headers.get("ETag"), body["d"]


class TestETag(ETagCase):
    def test_entity_carries_an_etag_in_header_and_payload(self):
        order = self.an_order()
        etag, entity = self.read(order)
        self.assertTrue(etag, "a concurrency-controlled entity must carry an ETag")
        self.assertTrue(etag.startswith('W/"datetime\''), etag)
        self.assertEqual(entity["__metadata"]["etag"], etag)

        # a type without a concurrency property has no ETag, as in Gateway
        status, headers, item = self.get(
            SRV + "/A_SalesOrderItem(SalesOrder='%s',SalesOrderItem='000010')?$format=json"
            % order)
        self.assertEqual(status, 200)
        self.assertNotIn("etag", item["d"]["__metadata"])
        self.assertIsNone(headers.get("ETag"))

    def test_the_etag_moves_when_the_entity_changes(self):
        order = self.an_order()
        headers = self.csrf_token()
        before, _ = self.read(order)
        seen = {before}
        for index in range(3):
            status, resp_headers, _ = self.request(
                "PATCH", SRV + "/A_SalesOrder('%s')" % order,
                body={"PurchaseOrderByCustomer": "PO-%d" % index},
                headers=dict(headers, **{"If-Match": "*"}))
            self.assertEqual(status, 204)
            self.assertIn("ETag", resp_headers, "an update answers with the new ETag")
            seen.add(resp_headers["ETag"])
        self.assertEqual(len(seen), 4, "every change must produce a fresh ETag")

    def test_conditional_read(self):
        order = self.an_order()
        etag, _ = self.read(order)

        status, headers, body = self.get(
            SRV + "/A_SalesOrder('%s')?$format=json" % order,
            headers={"If-None-Match": etag})
        self.assertEqual(status, 304)
        self.assertEqual(headers.get("ETag"), etag)
        self.assertIn(body, ("", b""), "304 must not carry a body")

        # change it, and the same validator no longer matches
        self.request("PATCH", SRV + "/A_SalesOrder('%s')" % order,
                     body={"PurchaseOrderByCustomer": "PO-CHANGED"},
                     headers=dict(self.csrf_token(), **{"If-Match": etag}))
        status, _, _ = self.get(SRV + "/A_SalesOrder('%s')?$format=json" % order,
                                headers={"If-None-Match": etag})
        self.assertEqual(status, 200)

    def test_update_with_a_current_etag_succeeds(self):
        order = self.an_order()
        etag, _ = self.read(order)
        status, headers, _ = self.request(
            "PATCH", SRV + "/A_SalesOrder('%s')" % order,
            body={"PurchaseOrderByCustomer": "PO-CURRENT"},
            headers=dict(self.csrf_token(), **{"If-Match": etag}))
        self.assertEqual(status, 204)
        self.assertNotEqual(headers.get("ETag"), etag)
        _, entity = self.read(order)
        self.assertEqual(entity["PurchaseOrderByCustomer"], "PO-CURRENT")

    def test_update_with_a_stale_etag_is_refused(self):
        order = self.an_order()
        stale, _ = self.read(order)
        headers = self.csrf_token()
        self.request("PATCH", SRV + "/A_SalesOrder('%s')" % order,
                     body={"PurchaseOrderByCustomer": "PO-FIRST"},
                     headers=dict(headers, **{"If-Match": stale}))

        status, _, body = self.request(
            "PATCH", SRV + "/A_SalesOrder('%s')" % order,
            body={"PurchaseOrderByCustomer": "PO-SECOND"},
            headers=dict(headers, **{"If-Match": stale}))
        self.assertEqual(status, 412)
        self.assertIn("changed by another user", body["error"]["message"]["value"])

        _, entity = self.read(order)
        self.assertEqual(entity["PurchaseOrderByCustomer"], "PO-FIRST",
                         "a refused update must not have been applied")

    def test_if_match_star_matches_any_version(self):
        order = self.an_order()
        status, _, _ = self.request(
            "PATCH", SRV + "/A_SalesOrder('%s')" % order,
            body={"PurchaseOrderByCustomer": "PO-STAR"},
            headers=dict(self.csrf_token(), **{"If-Match": "*"}))
        self.assertEqual(status, 204)

    def test_missing_if_match_is_allowed_by_default(self):
        order = self.an_order()
        status, _, _ = self.request(
            "PATCH", SRV + "/A_SalesOrder('%s')" % order,
            body={"PurchaseOrderByCustomer": "PO-NOVALIDATOR"},
            headers=self.csrf_token())
        self.assertEqual(status, 204)

    def test_delete_honours_the_validator(self):
        headers = self.csrf_token()
        status, _, created = self.request("POST", SRV + "/A_SalesOrder", headers=headers, body={
            "SalesOrderType": "OR", "SalesOrganization": "1710", "SoldToParty": "1000001",
            "DistributionChannel": "10", "OrganizationDivision": "00"})
        self.assertEqual(status, 201)
        order = created["d"]["SalesOrder"]
        etag, _ = self.read(order)

        self.request("PATCH", SRV + "/A_SalesOrder('%s')" % order,
                     body={"PurchaseOrderByCustomer": "PO-MOVED"},
                     headers=dict(headers, **{"If-Match": "*"}))

        status, _, _ = self.request("DELETE", SRV + "/A_SalesOrder('%s')" % order,
                                    headers=dict(headers, **{"If-Match": etag}))
        self.assertEqual(status, 412)
        status, _, _ = self.get(SRV + "/A_SalesOrder('%s')?$format=json" % order)
        self.assertEqual(status, 200, "a refused delete must leave the entity alone")

        current, _ = self.read(order)
        status, _, _ = self.request("DELETE", SRV + "/A_SalesOrder('%s')" % order,
                                    headers=dict(headers, **{"If-Match": current}))
        self.assertEqual(status, 204)

    def test_failed_precondition_rolls_a_changeset_back(self):
        order = self.an_order()
        stale, _ = self.read(order)
        headers = self.csrf_token()
        self.request("PATCH", SRV + "/A_SalesOrder('%s')" % order,
                     body={"PurchaseOrderByCustomer": "PO-BASE"},
                     headers=dict(headers, **{"If-Match": "*"}))

        body = (
            "--batch_etag\r\n"
            "Content-Type: multipart/mixed; boundary=changeset_etag\r\n\r\n"
            "--changeset_etag\r\n"
            "Content-Type: application/http\r\n"
            "Content-Transfer-Encoding: binary\r\n\r\n"
            "PATCH A_SalesOrder('%s') HTTP/1.1\r\n"
            "Content-Type: application/json\r\n"
            "If-Match: *\r\n\r\n"
            '{"PurchaseOrderByCustomer":"PO-INSIDE-BATCH"}\r\n'
            "--changeset_etag\r\n"
            "Content-Type: application/http\r\n"
            "Content-Transfer-Encoding: binary\r\n\r\n"
            "PATCH A_SalesOrder('%s') HTTP/1.1\r\n"
            "Content-Type: application/json\r\n"
            "If-Match: %s\r\n\r\n"
            '{"PurchaseOrderByCustomer":"PO-STALE"}\r\n'
            "--changeset_etag--\r\n"
            "--batch_etag--\r\n"
        ) % (order, order, stale)

        batch_headers = dict(headers)
        batch_headers["Content-Type"] = "multipart/mixed; boundary=batch_etag"
        status, _, raw = self.request("POST", SRV + "/$batch", body=body,
                                      headers=batch_headers, raw=True)
        self.assertEqual(status, 202)
        text = raw.decode()
        self.assertIn("HTTP/1.1 412 Precondition Failed", text)
        self.assertNotIn("HTTP/1.1 204", text)

        _, entity = self.read(order)
        self.assertEqual(entity["PurchaseOrderByCustomer"], "PO-BASE",
                         "the whole changeset must have been rolled back")

    def test_precondition_scenario(self):
        status, _, body = self.get(SRV + "/A_SalesOrder?$format=json",
                                   headers={"sap-mock-scenario": "precondition"})
        self.assertEqual(status, 412)
        self.assertIn("changed by another user", body["error"]["message"]["value"])


class TestETagRequired(ETagCase):
    """A service configured to insist on a validator, as newer Gateway does."""

    config_kwargs = {"require_if_match": True, "csrf": False}

    def test_modifying_without_if_match_is_refused(self):
        order = self.an_order()
        status, _, body = self.request(
            "PATCH", SRV + "/A_SalesOrder('%s')" % order,
            body={"PurchaseOrderByCustomer": "PO-NO-VALIDATOR"},
            headers={"Content-Type": "application/json"})
        self.assertEqual(status, 428)
        self.assertIn("If-Match", body["error"]["message"]["value"])

    def test_modifying_with_the_validator_still_works(self):
        order = self.an_order()
        etag, _ = self.read(order)
        status, _, _ = self.request(
            "PATCH", SRV + "/A_SalesOrder('%s')" % order,
            body={"PurchaseOrderByCustomer": "PO-WITH-VALIDATOR"},
            headers={"Content-Type": "application/json", "If-Match": etag})
        self.assertEqual(status, 204)

    def test_a_type_without_concurrency_needs_no_validator(self):
        order = self.an_order()
        status, _, _ = self.request(
            "PATCH",
            SRV + "/A_SalesOrderItem(SalesOrder='%s',SalesOrderItem='000010')" % order,
            body={"SalesOrderItemText": "no validator needed"},
            headers={"Content-Type": "application/json"})
        self.assertEqual(status, 204)


if __name__ == "__main__":
    unittest.main(verbosity=2)
