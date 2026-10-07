"""The control plane told something it cannot use, and what a reset forgets (#158).

Three findings from one review, all the same shape: `/_mock` answering in a way
that sent whoever was driving it to look somewhere else. A 500 with a Python
message reads as a broken mock, not a mistyped `limit`. A fault rule accepted
with a 201 and failing other requests later reads as the fault that was asked
for. A request log that survives a reset reads as the test's own requests.

`Content-Length` is here because it is the same complaint one layer down: a
request the mock could not read got no answer at all, or waited for ever.
"""
from __future__ import annotations

import socket
import unittest

from support import MockServerCase, SRV

RFC = "/sap/bc/rfc/BAPI_TRANSACTION_COMMIT"


class ControlCase(MockServerCase):
    def refused(self, method, path, body=None):
        """The message of a 400, having checked that it was one."""
        status, _, answer = self.request(method, path, body=body)
        self.assertEqual(status, 400, answer)
        message = answer["error"]["message"]["value"]
        self.assertNotIn("Unexpected mock failure", message)
        return message


class TestAResetForgetsWhatWasAsked(ControlCase):
    def logged(self):
        return self.get("/_mock/requests?limit=100")[2]["results"]

    def test_the_request_log_is_empty_afterwards(self):
        self.get(SRV + "/A_SalesOrder?$top=1&$format=json")
        self.assertTrue(self.logged(), "something to forget")

        self.request("POST", "/_mock/reset")

        self.assertEqual(self.logged(), [],
                         "the next test would read these as its own")

    def test_the_first_request_after_it_is_request_one(self):
        self.get(SRV + "/A_SalesOrder?$top=1&$format=json")
        self.get(SRV + "/A_SalesOrder?$top=1&$format=json")
        self.request("POST", "/_mock/reset")

        self.get(SRV + "/A_SalesOrder?$top=1&$format=json")

        self.assertEqual([row["id"] for row in self.logged()], [1])

    def test_the_first_function_call_after_it_is_call_one(self):
        """`rfc_log` was emptied already; its counter was not."""
        headers = self.csrf_token()
        self.request("POST", RFC, body={}, headers=headers)
        self.request("POST", RFC, body={}, headers=headers)
        self.request("POST", "/_mock/reset")

        self.request("POST", RFC, body={}, headers=self.csrf_token())

        calls = self.get("/_mock/rfc-log?limit=100")[2]["results"]
        self.assertEqual([row["id"] for row in calls], [1])

    def test_a_reset_before_anything_was_logged_is_still_a_reset(self):
        """No counter exists yet on a fresh database; that is not an error."""
        self.request("POST", "/_mock/reset")
        status, _, body = self.request("POST", "/_mock/reset")

        self.assertEqual(status, 200)
        self.assertTrue(body["reset"])


class TestInputItCannotUseIsRefusedInWords(ControlCase):
    def test_a_limit_that_is_not_a_number(self):
        for path in ("/_mock/requests", "/_mock/rfc-log", "/_mock/idocs"):
            with self.subTest(path=path):
                self.assertEqual(self.refused("GET", path + "?limit=abc"),
                                 "limit is a whole number, not 'abc'")

    def test_a_negative_limit(self):
        """SQLite reads LIMIT -1 as no limit, which is not what was typed."""
        self.assertIn("whole number",
                      self.refused("GET", "/_mock/requests?limit=-1"))

    def test_a_limit_that_is_a_number_still_limits(self):
        for _ in range(3):
            self.get(SRV + "/A_SalesOrder?$top=1&$format=json")

        rows = self.get("/_mock/requests?limit=2")[2]["results"]

        self.assertEqual(len(rows), 2)

    def test_a_reset_seeded_with_a_word(self):
        for field in ("seed", "orders", "purchaseOrders"):
            with self.subTest(field=field):
                self.assertEqual(
                    self.refused("POST", "/_mock/reset", {field: "many"}),
                    "%s is a whole number, not 'many'" % field)

    def test_a_reset_refused_has_reset_nothing(self):
        _, _, before = self.get(SRV + "/A_SalesOrder/$count", raw=True)
        headers = self.csrf_token()
        self.request("POST", SRV + "/A_SalesOrder", headers=headers, body={
            "SalesOrderType": "OR", "SalesOrganization": "1710",
            "SoldToParty": "1000001", "DistributionChannel": "10",
            "OrganizationDivision": "00"})

        self.refused("POST", "/_mock/reset", {"orders": "many"})

        _, _, after = self.get(SRV + "/A_SalesOrder/$count", raw=True)
        self.assertEqual(int(after), int(before) + 1,
                         "half a reset would be worse than none")
        self.request("POST", "/_mock/reset")

    def test_a_body_that_is_json_but_not_an_object(self):
        kinds = (([1], "an array"), (5, "a number"), ('"x"', "a string"),
                 ("null", "null"), ("true", "a boolean"))
        for path in ("reset", "faults", "idoc-posting", "bapi-behaviour",
                     "open-items", "advance"):
            for body, kind in kinds:
                with self.subTest(path=path, body=body):
                    if not isinstance(body, str):
                        body = str(body)
                    self.assertEqual(
                        self.refused("POST", "/_mock/" + path, body),
                        "The request body must be a JSON object, not " + kind)

    def test_a_rule_count_that_is_not_a_number(self):
        rules = (("idoc-posting", {"status": "51", "count": "twice"}),
                 ("bapi-behaviour", {"function": "BAPI_TRANSACTION_COMMIT",
                                     "message": "No", "count": "twice"}))
        for path, rule in rules:
            with self.subTest(path=path):
                self.assertEqual(self.refused("POST", "/_mock/" + path, rule),
                                 "count is a whole number, not 'twice'")


class TestAFaultRuleThatCouldNotBeApplied(ControlCase):
    """Refused when it is posted, not when it meets somebody's request."""

    def tearDown(self):
        self.request("DELETE", "/_mock/faults")

    def test_each_is_refused_and_names_its_field(self):
        rules = (({"status": "abc"}, "status is a whole number, not 'abc'"),
                 ({"status": 5.5}, "status is a whole number, not 5.5"),
                 ({"status": 42}, "status is an HTTP status code, not 42"),
                 ({"status": 503, "count": "twice"},
                  "count is a whole number, not 'twice'"),
                 ({"status": 503, "delay_ms": "slowly"},
                  "delay_ms is a whole number, not 'slowly'"),
                 ({"status": 503, "method": 7}, "method is text, not 7"),
                 ({"status": 503, "message": ["no"]},
                  "message is text or a message object, not ['no']"))
        for rule, said in rules:
            with self.subTest(rule=rule):
                self.assertEqual(self.refused("POST", "/_mock/faults", rule),
                                 said)

    def test_a_pattern_that_does_not_compile(self):
        message = self.refused("POST", "/_mock/faults",
                               {"match": "A_Sales(Order", "status": 503})

        self.assertIn("match is not a regular expression", message)

    def test_a_refused_rule_is_not_kept(self):
        self.refused("POST", "/_mock/faults", {"status": "abc"})

        self.assertEqual(self.get("/_mock/faults")[2]["results"], [])
        status, _, _ = self.get(SRV + "/A_SalesOrder?$top=1&$format=json")
        self.assertEqual(status, 200, "and fails nobody's request")

    def test_a_status_written_as_text_is_still_a_status(self):
        """`"503"` has always worked; refusing words must not refuse this."""
        status, _, rule = self.request(
            "POST", "/_mock/faults",
            body={"match": "A_SalesOrder", "status": "503", "count": "1"})
        self.assertEqual(status, 201)
        self.assertEqual((rule["status"], rule["count"]), (503, 1))

        status, _, _ = self.get(SRV + "/A_SalesOrder?$top=1&$format=json")
        self.assertEqual(status, 503)
        status, _, _ = self.get(SRV + "/A_SalesOrder?$top=1&$format=json")
        self.assertEqual(status, 200, "a count of one fires once")


class TestAContentLengthThatIsNotALength(ControlCase):
    def exchange(self, content_length, timeout=5):
        """Everything the mock sends back before it closes the connection."""
        request = ("POST /_mock/faults HTTP/1.1\r\nHost: mock\r\n"
                   "Content-Length: %s\r\n\r\n" % content_length).encode()
        with socket.create_connection(("127.0.0.1", self.port)) as sock:
            sock.settimeout(timeout)
            sock.sendall(request)
            received = b""
            while True:
                chunk = sock.recv(65536)     # times out if the mock hangs
                if not chunk:
                    return received.decode("utf-8", "replace")
                received += chunk

    def test_it_is_refused_and_the_connection_is_closed(self):
        for sent in ("-1", "abc", "1e3", "+5", "5 5"):
            with self.subTest(content_length=sent):
                answer = self.exchange(sent)

                self.assertTrue(answer.startswith("HTTP/1.1 400 "), answer[:60])
                self.assertIn("Content-Length is a count of bytes, not %r" % sent,
                              answer)

    def test_no_rule_was_made_out_of_whatever_followed(self):
        self.exchange("-1")

        self.assertEqual(self.get("/_mock/faults")[2]["results"], [])

    def test_the_mock_is_still_answering_afterwards(self):
        self.exchange("abc")

        status, _, health = self.get("/_mock/health")
        self.assertEqual((status, health["status"]), (200, "UP"))


if __name__ == "__main__":
    unittest.main()
