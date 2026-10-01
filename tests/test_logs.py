"""What the control-plane logs return, against what they record.

`request_log` and `rfc_log` each store more columns than their endpoint used
to select, and the two most useful - a BAPI's `RETURN` table and the body a
caller sent - were among the ones left out. These tests pin the opt-in that
returns them, and the redaction that keeps a credential out of the answer.
"""
from __future__ import annotations

import base64
import unittest

from support import MockServerCase, SRV


class TestTheLogEndpoints(MockServerCase):
    config_kwargs = {"csrf": False}

    def test_the_default_shape_is_unchanged(self):
        """A client already parsing these rows keeps what it was written against."""
        self.get(SRV + "/A_SalesOrder?$top=1&$format=json")
        _, _, body = self.get("/_mock/requests?limit=1")
        [row] = body["results"]
        self.assertEqual(set(row), {"id", "ts", "method", "path", "query",
                                    "status", "duration_ms"})

        self.request("POST", "/sap/bc/rfc/BAPI_TRANSACTION_COMMIT", body={})
        _, _, body = self.get("/_mock/rfc-log?limit=1")
        [row] = body["results"]
        self.assertEqual(set(row), {"id", "ts", "function_name", "protocol"})

    def test_a_refused_bapi_is_distinguishable_from_one_that_worked(self):
        """The point of the change: a business error is invisible without it.

        A BAPI reports a business error by returning normally with HTTP 200
        and a `RETURN` row of type E. Through the narrow log the failure and
        the success are the same two fields, so a client watching the control
        plane cannot see that anything went wrong - which is the opposite of
        what a fault-injection endpoint is for.
        """
        self.request("POST", "/_mock/bapi-behaviour",
                     body={"function": "BAPI_PO_CREATE1", "type": "E",
                           "message": "Injected: posting period closed"})
        status, _, _ = self.request("POST", "/sap/bc/rfc/BAPI_PO_CREATE1",
                                    body={"POHEADER": {"VENDOR": "1000"}})
        self.assertEqual(status, 200, "a business error is still HTTP 200")

        _, _, narrow = self.get("/_mock/rfc-log?limit=1")
        self.assertNotIn("response", narrow["results"][0])

        _, _, body = self.get("/_mock/rfc-log?limit=1&verbose=1")
        [row] = body["results"]
        self.assertEqual(row["function_name"], "BAPI_PO_CREATE1")
        self.assertEqual(row["request"], {"POHEADER": {"VENDOR": "1000"}})
        [message] = row["response"]["RETURN"]
        self.assertEqual(message["TYPE"], "E")
        self.assertEqual(message["MESSAGE"], "Injected: posting period closed")
        self.request("DELETE", "/_mock/bapi-behaviour")

    def test_the_payloads_come_back_parsed_not_as_json_text(self):
        """The caller's question is what was in RETURN, not how it serialised."""
        self.request("POST", "/sap/bc/rfc/BAPI_TRANSACTION_COMMIT", body={})
        _, _, body = self.get("/_mock/rfc-log?limit=1&verbose=1")
        [row] = body["results"]
        self.assertIsInstance(row["response"], dict)
        self.assertIsInstance(row["request"], dict)

    def test_a_request_body_is_returned_when_asked_for(self):
        self.request("POST", "/sap/bc/rfc/BAPI_SALESORDER_GETSTATUS",
                     body={"SALESDOCUMENT": "0000004711"})
        _, _, body = self.get("/_mock/requests?limit=5&verbose=1")
        row = next(r for r in body["results"]
                   if r["path"].endswith("BAPI_SALESORDER_GETSTATUS"))
        self.assertIn("0000004711", row["body"])
        self.assertIsInstance(row["headers"], dict)

    def test_verbose_takes_the_spellings_a_caller_will_try(self):
        self.get(SRV + "/A_SalesOrder?$top=1&$format=json")
        for spelling in ("1", "true", "yes", "on", "TRUE"):
            _, _, body = self.get("/_mock/requests?limit=1&verbose=" + spelling)
            self.assertIn("body", body["results"][0], spelling)
        for spelling in ("0", "false", "no", ""):
            _, _, body = self.get("/_mock/requests?limit=1&verbose=" + spelling)
            self.assertNotIn("body", body["results"][0], spelling)


class TestLogsDoNotLeakCredentials(MockServerCase):
    """`/_mock/requests` is not behind `--auth`, and it stores every header."""

    config_kwargs = {"basic_auth": "sapuser:secret", "csrf": False}

    def test_the_authorization_header_is_redacted(self):
        token = base64.b64encode(b"sapuser:secret").decode()
        status, _, _ = self.get(SRV + "/A_SalesOrder?$top=1&$format=json",
                                headers={"Authorization": "Basic " + token})
        self.assertEqual(status, 200)

        _, _, body = self.get("/_mock/requests?limit=10&verbose=1",
                              headers={"Authorization": "Basic " + token})
        rows = body["results"]
        self.assertTrue(rows)
        for row in rows:
            for name, value in row["headers"].items():
                self.assertNotIn(
                    token, str(value),
                    "header %s handed back the caller's credential" % name)
                self.assertNotIn("secret", str(value), name)

    def test_the_header_is_marked_rather_than_dropped(self):
        """A client debugging an auth failure needs to know it was sent.

        An absent key cannot be told apart from a header that was never set,
        which is the question being debugged.
        """
        token = base64.b64encode(b"sapuser:secret").decode()
        self.get(SRV + "/A_SalesOrder?$top=1&$format=json",
                 headers={"Authorization": "Basic " + token})
        _, _, body = self.get("/_mock/requests?limit=10&verbose=1",
                              headers={"Authorization": "Basic " + token})
        sent = [r for r in body["results"]
                if any(k.lower() == "authorization" for k in r["headers"])]
        self.assertTrue(sent, "the header was stored under some spelling")
        for row in sent:
            value = next(v for k, v in row["headers"].items()
                         if k.lower() == "authorization")
            self.assertIn("redact", str(value).lower())


if __name__ == "__main__":
    unittest.main()
