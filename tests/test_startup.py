"""Starting up: the port opens without waiting on a name server.

The standard library's `HTTPServer.server_bind` reverse-resolves the address
it just bound, to fill in a `server_name` that nothing here reads. Where the
resolver is slow to answer for that address - a CI runner, a container with no
reverse record - the port does not open until the lookup returns, and whoever
is polling `/_mock/health` gives up first and reports a start that failed for
no visible reason (#127). mock-edi and mock-bank met this before mock-sap did.

These tests replace `socket.getfqdn` rather than the network: a resolver that
is slow cannot be arranged on a laptop, and one that is never asked cannot be
slow.
"""
from __future__ import annotations

import os
import socket
import sys
import threading
import time
import unittest
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mocksap.server import Config, make_server  # noqa: E402


class StartingUpDoesNotWaitForAResolver(unittest.TestCase):
    def setUp(self):
        self.real_getfqdn = socket.getfqdn
        self.addCleanup(setattr, socket, "getfqdn", self.real_getfqdn)

    def serve(self, host="127.0.0.1"):
        httpd = make_server(Config(host=host, port=0, db_path=":memory:", quiet=True))
        self.addCleanup(httpd.mock.close)
        self.addCleanup(httpd.server_close)
        return httpd

    def test_the_bound_address_is_never_looked_up(self):
        looked_up = []

        def getfqdn(name=""):
            looked_up.append(name)
            return self.real_getfqdn(name)

        socket.getfqdn = getfqdn
        self.serve()
        self.serve("0.0.0.0")
        self.assertEqual(looked_up, [])

    def test_so_a_resolver_that_does_not_answer_cannot_delay_the_bind(self):
        # Three seconds stands in for the twenty and more a macOS runner
        # spent; the assertion is that none of it is spent, not some of it.
        socket.getfqdn = lambda name="": time.sleep(3) or self.real_getfqdn(name)
        start = time.monotonic()
        self.serve("0.0.0.0")
        self.assertLess(time.monotonic() - start, 1.0)

    def test_and_the_server_still_knows_where_it_is_listening(self):
        # server_name and server_port are what the override sets in place of
        # the lookup; anything built on HTTPServer expects them to exist.
        httpd = self.serve()
        self.assertEqual(httpd.server_name, "127.0.0.1")
        self.assertEqual(httpd.server_port, httpd.socket.getsockname()[1])
        self.assertNotEqual(httpd.server_port, 0)

    def test_and_it_answers_a_health_check(self):
        httpd = self.serve()
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(httpd.shutdown)
        url = "http://127.0.0.1:%d/_mock/health" % httpd.server_port
        with urllib.request.urlopen(url, timeout=5) as resp:
            self.assertEqual(resp.status, 200)


if __name__ == "__main__":
    unittest.main()
