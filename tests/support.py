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
        cls.httpd.mock.close()

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


# --------------------------------------------------------------------------
# IDoc payloads
#
# These are the documents a client sends, not fixtures of the mock's own
# making, so they live beside the harness rather than in whichever test
# module happened to need one first. Three modules build on them: posting a
# statement, what posting an IDoc is remembered to have decided, and
# generating a remittance advice from the clearing that resulted.
# --------------------------------------------------------------------------

def invoic(reference, gross="1190.00", net="1000.00", tax="190.00",
           supplier="1000009", currency="EUR"):
    return (
        '<?xml version="1.0" encoding="utf-8"?><INVOIC02><IDOC BEGIN="1">'
        '<EDI_DC40 SEGMENT="1"><IDOCTYP>INVOIC02</IDOCTYP>'
        "<MESTYP>INVOIC</MESTYP></EDI_DC40>"
        '<E1EDK01 SEGMENT="1"><CURCY>%s</CURCY><ZTERM>NT30</ZTERM>'
        "<BELNR>%s</BELNR></E1EDK01>"
        '<E1EDK02 SEGMENT="1"><QUALF>009</QUALF><BELNR>%s</BELNR></E1EDK02>'
        '<E1EDK03 SEGMENT="1"><IDDAT>026</IDDAT><DATUM>20260927</DATUM></E1EDK03>'
        '<E1EDKA1 SEGMENT="1"><PARVW>LF</PARVW><LIFNR>%s</LIFNR></E1EDKA1>'
        '<E1EDP01 SEGMENT="1"><POSEX>000010</POSEX><MENGE>1.000</MENGE>'
        "<MENEE>PC</MENEE><NETWR>%s</NETWR><VGBEL>4500000100</VGBEL>"
        "<VGPOS>00010</VGPOS></E1EDP01>"
        '<E1EDS01 SEGMENT="1"><SUMID>010</SUMID><SUMME>%s</SUMME></E1EDS01>'
        '<E1EDS01 SEGMENT="1"><SUMID>011</SUMID><SUMME>%s</SUMME></E1EDS01>'
        '<E1EDS01 SEGMENT="1"><SUMID>205</SUMID><SUMME>%s</SUMME></E1EDS01>'
        "</IDOC></INVOIC02>") % (currency, reference, reference, supplier,
                                 net, gross, net, tax)


def line(number, amount, reference=None, note=None, currency="EUR"):
    """One statement line: a trailing minus is money out.

    `currency` is the line's own `CUXWAERZ`. It is a parameter because a
    number without one is not an amount (#88): a line for 1190.00 pays a
    payable of 1190.00 only if both are in the same money.
    """
    parts = ['<E1IDPF1 SEGMENT="1"><LINLINEIT>%s</LINLINEIT>' % number]
    if reference is not None:
        parts.append('<E1EDP02 SEGMENT="1"><QUALF>009</QUALF><BELNR>%s</BELNR>'
                     "</E1EDP02>" % reference)
    if note is not None:
        chunks = [note[i:i + 70] for i in range(0, len(note), 70)] or [""]
        inner = "".join("<TXT%02d>%s</TXT%02d>" % (n, chunk, n)
                        for n, chunk in enumerate(chunks, start=1))
        parts.append('<E1IDT01 SEGMENT="1">%s</E1IDT01>' % inner)
    parts.append('<E1IDPU5 SEGMENT="1"><MOAQUAL>001</MOAQUAL>'
                 "<MOABETR>%s</MOABETR><CUXWAERZ>%s</CUXWAERZ></E1IDPU5>"
                 % (amount, currency))
    parts.append("</E1IDPF1>")
    return "".join(parts)


def balances(opening=None, closing=None, debits=None, credits_=None):
    amounts = []
    for qualifier, value in (("019", opening), ("021", closing),
                             ("023", debits), ("024", credits_)):
        if value is not None:
            amounts.append('<E1IDPU5 SEGMENT="1"><MOAQUAL>%s</MOAQUAL>'
                           "<MOABETR>%s</MOABETR><CUXWAERZ>EUR</CUXWAERZ>"
                           "</E1IDPU5>" % (qualifier, value))
    if not amounts:
        return ""
    return ('<E1IDPF1 SEGMENT="1"><LINLINEIT>000900</LINLINEIT>%s</E1IDPF1>'
            % "".join(amounts))


def finsta(lines="", statement="00042", date="20260927", account="0007000063",
           opening=None, closing=None, debits=None, credits_=None,
           currency="EUR"):
    return (
        '<?xml version="1.0" encoding="utf-8"?><FINSTA01><IDOC BEGIN="1">'
        '<EDI_DC40 SEGMENT="1"><IDOCTYP>FINSTA01</IDOCTYP>'
        "<MESTYP>FINSTA</MESTYP></EDI_DC40>"
        '<E1IDKU1 SEGMENT="1"><BGMREF>%s</BGMREF>'
        '<E1EDK03 SEGMENT="1"><IDDAT>026</IDDAT><DATUM>%s</DATUM></E1EDK03>'
        '<E1IDB02 SEGMENT="1"><FIIBKENN>37040044</FIIBKENN>'
        "<FIIKONTO>%s</FIIKONTO><FIIBLAND>DE</FIIBLAND><FIIKWAER>%s</FIIKWAER>"
        "</E1IDB02>%s%s</E1IDKU1></IDOC></FINSTA01>"
    ) % (statement, date, account, currency, lines,
         balances(opening, closing, debits, credits_))
