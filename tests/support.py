"""Shared harness for the end-to-end tests.

Every test in this suite talks to a real mock over real HTTP: MockServerCase
starts one in a background thread on an ephemeral port, and subclasses point
`config_kwargs` at whatever configuration the surface under test needs.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mocksap.server import Config, make_server  # noqa: E402

SRV = "/sap/opu/odata/sap/API_SALES_ORDER_SRV"
BP_SRV = "/sap/opu/odata/sap/API_BUSINESS_PARTNER_SRV"


class MockServerCase(unittest.TestCase):
    config_kwargs: dict = {}

    @classmethod
    def setUpClass(cls):
        kwargs = dict(host="127.0.0.1", port=0, db_path=":memory:", quiet=True)
        kwargs.update(cls.config_kwargs)
        cls.httpd = make_server(Config(**kwargs))
        cls.port = cls.httpd.server_address[1]
        cls.base = "http://127.0.0.1:%d" % cls.port
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    # -- helpers
    def request(self, method, path, body=None, headers=None, raw=False):
        url = self.base + path.replace(" ", "%20")
        data = body
        if isinstance(data, (dict, list)):
            data = json.dumps(data).encode()
        elif isinstance(data, str):
            data = data.encode()
        req = urllib.request.Request(url, data=data, method=method)
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        try:
            with urllib.request.urlopen(req) as resp:
                payload = resp.read()
                return resp.status, dict(resp.headers), payload if raw else _maybe_json(payload)
        except urllib.error.HTTPError as err:
            payload = err.read()
            return err.code, dict(err.headers), payload if raw else _maybe_json(payload)

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def csrf_token(self):
        status, headers, _ = self.get(SRV + "/", headers={"X-CSRF-Token": "Fetch"})
        self.assertEqual(status, 200)
        token = headers.get("x-csrf-token") or headers.get("X-CSRF-Token")
        self.assertTrue(token)
        return {"X-CSRF-Token": token, "Content-Type": "application/json"}


def _maybe_json(payload: bytes):
    try:
        return json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return payload.decode("utf-8", "replace")
