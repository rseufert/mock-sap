"""The startup banner: the URLs it offers are the ones the server is on.

`--port 0` asks the operating system for a free port, which is how several
mocks run at once and how CI avoids a clash. The banner was built from the
parsed arguments, so it printed the port that was *asked* for: eighteen URLs
ending `:0`, none of them reachable, and - since the banner is the only place
the port is reported - no way to find the server short of `lsof` (#129).

So the banner is built from the server. `main` prints and then blocks in
`serve_forever`, which would put these assertions behind a subprocess; the
lines are assembled by `banner(args, httpd)` instead, and read back here from
a mock bound to a real socket. The port is checked against
`socket.getsockname()` rather than the `server_port` the banner reads, so the
two cannot agree by sharing a mistake.
"""
from __future__ import annotations

import os
import re
import sys
import threading
import unittest
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mocksap.__main__ import banner, build_parser  # noqa: E402
from mocksap.server import Config, make_server  # noqa: E402

URL = re.compile(r"http://(?P<host>[^/\s:]+):(?P<port>\d+)(?P<path>\S*)")


class TheBannerReportsTheBoundPort(unittest.TestCase):
    def serve(self, *argv):
        args = build_parser().parse_args(["--port", "0", "--quiet"] + list(argv))
        httpd = make_server(Config(**vars(args)))
        self.addCleanup(httpd.mock.close)
        self.addCleanup(httpd.server_close)
        return args, httpd

    def banner(self, *argv):
        args, httpd = self.serve(*argv)
        return banner(args, httpd), httpd

    def test_every_url_carries_the_port_the_socket_is_actually_on(self):
        lines, httpd = self.banner()
        bound = httpd.socket.getsockname()[1]
        self.assertNotEqual(bound, 0)
        found = URL.findall("\n".join(lines))
        # Eighteen at the time of writing; the point is that none is missed.
        self.assertGreater(len(found), 1)
        self.assertEqual({int(m[1]) for m in found}, {bound})

    def test_including_when_a_different_port_was_asked_for(self):
        # The requested port must not be what is printed, which is the whole
        # bug: `--port 0` asked for 0 and got one the caller cannot guess.
        args, httpd = self.serve()
        args.port = 8000
        bound = httpd.socket.getsockname()[1]
        ports = {int(m[1]) for m in URL.findall("\n".join(banner(args, httpd)))}
        self.assertEqual(ports, {bound})

    def test_and_a_client_handed_the_banner_can_reach_every_one_of_them(self):
        # The done condition from the issue, read literally: not that the
        # numbers match, but that a reader who copies a line out of the banner
        # gets an answer. Printed `:0` every one of these is a refused
        # connection.
        lines, httpd = self.banner()
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.shutdown)
        urls = [m.group(0) for m in URL.finditer("\n".join(lines))]
        reached = []
        for url in urls:
            if "<" in url:  # the RFC line names a function, not a resource
                continue
            with urllib.request.urlopen(url, timeout=5) as resp:
                self.assertEqual(resp.status, 200, url)
            reached.append(url)
        self.assertGreater(len(reached), 1)

    def test_but_the_host_stays_the_one_that_was_typed(self):
        # `localhost` binds to 127.0.0.1, so the socket and the argument
        # disagree - and the argument wins. The reader typed the host; nobody
        # typed the port, which is why only one of the two is read back off
        # the server.
        lines, httpd = self.banner("--host", "localhost")
        self.assertEqual(httpd.socket.getsockname()[0], "127.0.0.1")
        self.assertEqual({m[0] for m in URL.findall("\n".join(lines))}, {"localhost"})


if __name__ == "__main__":
    unittest.main()
