"""A minimal SAP-style client against the mock, using only the standard library.

Shows the flow any real SAP OData client has to implement: fetch a CSRF
token, keep the session cookie, then read and write.  Run the mock first:

    python3 -m mocksap --port 8000
    python3 examples/client.py            # or: python3 examples/client.py http://host:port
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from http.cookiejar import CookieJar
from urllib.parse import quote

BASE = (sys.argv[1] if len(sys.argv) > 1
        else os.environ.get("BASE", "http://127.0.0.1:8000"))
SRV = BASE + "/sap/opu/odata/sap/API_SALES_ORDER_SRV"


class SapClient:
    def __init__(self, base_url: str, client: str = "100"):
        self.base_url = base_url
        self.client = client
        self.token = None
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))

    def _call(self, method, url, body=None, headers=None, parse=True):
        data = json.dumps(body).encode() if isinstance(body, (dict, list)) else body
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Accept", "application/json")
        req.add_header("sap-client", self.client)
        if data:
            req.add_header("Content-Type", "application/json")
        if self.token and method not in ("GET", "HEAD"):
            req.add_header("X-CSRF-Token", self.token)
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        with self.opener.open(req) as resp:
            payload = resp.read()
            if resp.headers.get("x-csrf-token"):
                self.token = resp.headers["x-csrf-token"]
            if not parse or not payload:
                return resp.status, payload
            return resp.status, json.loads(payload.decode())

    def fetch_token(self):
        self._call("GET", SRV + "/", headers={"X-CSRF-Token": "Fetch"}, parse=False)
        return self.token

    def read(self, path, **options):
        query = "&".join("%s=%s" % (k.replace("_", "$", 1), quote(str(v)))
                         for k, v in options.items())
        _, body = self._call("GET", "%s/%s?%s&$format=json" % (SRV, path, query))
        return body["d"]

    def create(self, entity_set, payload, expand=None):
        url = "%s/%s" % (SRV, entity_set)
        if expand:
            url += "?$expand=" + expand
        _, body = self._call("POST", url, payload)
        return body["d"]


if __name__ == "__main__":
    sap = SapClient(BASE)
    print("CSRF token:", sap.fetch_token())

    page = sap.read("A_SalesOrder", _top=3, _orderby="TotalNetAmount desc",
                    _select="SalesOrder,SoldToParty,TotalNetAmount",
                    _inlinecount="allpages")
    print("total orders in system:", page["__count"])
    for row in page["results"]:
        print("  %s  customer %s  %s" % (
            row["SalesOrder"], row["SoldToParty"], row["TotalNetAmount"]))

    created = sap.create("A_SalesOrder", {
        "SalesOrderType": "OR", "SalesOrganization": "1710",
        "DistributionChannel": "10", "OrganizationDivision": "00",
        "SoldToParty": "1000001", "TransactionCurrency": "EUR",
        "to_Item": [
            {"Material": "TG11", "RequestedQuantity": "2",
             "RequestedQuantityUnit": "PC", "NetAmount": "1998.00"},
        ],
    }, expand="to_Item")
    print("created", created["SalesOrder"], "net", created["TotalNetAmount"],
          "items", [i["SalesOrderItem"] for i in created["to_Item"]["results"]])
