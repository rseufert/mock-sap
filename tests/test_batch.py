"""$batch: multipart parsing, changesets and their all-or-nothing rollback."""
from __future__ import annotations

import unittest

from support import MockServerCase, SRV


class TestBatch(MockServerCase):
    def _batch(self, payload: str):
        boundary = "batch_test"
        headers = self.csrf_token()
        headers["Content-Type"] = "multipart/mixed; boundary=" + boundary
        return self.request("POST", SRV + "/$batch", body=payload, headers=headers, raw=True)

    def test_batch_get_and_changeset(self):
        body = (
            "--batch_test\r\n"
            "Content-Type: application/http\r\n"
            "Content-Transfer-Encoding: binary\r\n\r\n"
            "GET A_SalesOrder?$top=1&$format=json HTTP/1.1\r\n"
            "Accept: application/json\r\n\r\n"
            "--batch_test\r\n"
            "Content-Type: multipart/mixed; boundary=changeset_1\r\n\r\n"
            "--changeset_1\r\n"
            "Content-Type: application/http\r\n"
            "Content-Transfer-Encoding: binary\r\n\r\n"
            "POST A_SalesOrder HTTP/1.1\r\n"
            "Content-Type: application/json\r\n\r\n"
            '{"SalesOrderType":"OR","SalesOrganization":"1710","SoldToParty":"1000001",'
            '"DistributionChannel":"10","OrganizationDivision":"00"}\r\n'
            "--changeset_1--\r\n"
            "--batch_test--\r\n"
        )
        status, headers, raw = self._batch(body)
        text = raw.decode()
        self.assertEqual(status, 202)
        self.assertIn("multipart/mixed", headers["Content-Type"])
        self.assertIn("HTTP/1.1 200 OK", text)
        self.assertIn("HTTP/1.1 201 Created", text)
        self.assertIn("changesetresponse_", text)

    def test_changeset_is_rolled_back_on_error(self):
        _, _, before = self.get(SRV + "/A_SalesOrder/$count", raw=True)
        body = (
            "--batch_test\r\n"
            "Content-Type: multipart/mixed; boundary=changeset_2\r\n\r\n"
            "--changeset_2\r\n"
            "Content-Type: application/http\r\n"
            "Content-Transfer-Encoding: binary\r\n\r\n"
            "POST A_SalesOrder HTTP/1.1\r\n"
            "Content-Type: application/json\r\n\r\n"
            '{"SalesOrderType":"OR","SalesOrganization":"1710","SoldToParty":"1000001"}\r\n'
            "--changeset_2\r\n"
            "Content-Type: application/http\r\n"
            "Content-Transfer-Encoding: binary\r\n\r\n"
            "POST A_SalesOrder HTTP/1.1\r\n"
            "Content-Type: application/json\r\n\r\n"
            '{"ThisPropertyIsWrong":"1"}\r\n'
            "--changeset_2--\r\n"
            "--batch_test--\r\n"
        )
        status, _, raw = self._batch(body)
        text = raw.decode()
        self.assertIn("HTTP/1.1 400 Bad Request", text)
        self.assertNotIn("HTTP/1.1 201 Created", text)
        _, _, after = self.get(SRV + "/A_SalesOrder/$count", raw=True)
        self.assertEqual(int(before), int(after), "changeset must roll back")

if __name__ == "__main__":
    unittest.main(verbosity=2)
