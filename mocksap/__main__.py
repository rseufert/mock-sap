"""Command line entry point: ``python -m mocksap`` / ``mock-sap``."""
from __future__ import annotations

import argparse
import sys

from . import __version__
from .server import Config, make_server
from .schema import SERVICES


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mock-sap",
        description="Run a black-box mock SAP endpoint (OData V2, BAPI/RFC, IDoc).")
    p.add_argument("--host", default="127.0.0.1", help="bind address (default: 127.0.0.1)")
    p.add_argument("--port", type=int, default=8000, help="port (default: 8000)")
    p.add_argument("--db", dest="db_path", default=":memory:",
                   help="SQLite file, or :memory: (default) for a throwaway system")
    p.add_argument("--client", default="100", help="SAP client (default: 100)")
    p.add_argument("--user", default="MOCKUSER",
                   help="user name written into administrative fields")
    p.add_argument("--auth", dest="basic_auth", metavar="USER:PASSWORD",
                   help="require HTTP basic authentication")
    p.add_argument("--oauth", metavar="CLIENT_ID:CLIENT_SECRET",
                   help="require an OAuth 2.0 bearer token, and serve the token "
                        "endpoint at /sap/bc/sec/oauth2/token")
    p.add_argument("--token-ttl", type=int, default=3600,
                   help="lifetime in seconds of an issued access token "
                        "(default: 3600; set it low to exercise refresh)")
    p.add_argument("--no-csrf", dest="csrf", action="store_false",
                   help="do not require an X-CSRF-Token on modifying requests")
    p.add_argument("--require-if-match", action="store_true",
                   help="refuse to modify a concurrency-controlled entity that "
                        "arrives without an If-Match header (428)")
    p.add_argument("--seed", dest="seed_value", type=int, default=42,
                   help="seed for the generated demo data (default: 42)")
    p.add_argument("--latency-ms", type=int, default=0,
                   help="artificial delay added to every request")
    p.add_argument("--error-rate", type=float, default=0.0,
                   help="fraction of requests answered with a 500 (0.0-1.0)")
    p.add_argument("--slow-ms", type=int, default=3000,
                   help="delay used by the 'slow' scenario (default: 3000)")
    p.add_argument("--no-request-log", dest="log_requests", action="store_false",
                   help="do not record requests in the request_log table")
    p.add_argument("-q", "--quiet", action="store_true", help="suppress the access log")
    p.add_argument("--version", action="version", version="mock-sap " + __version__)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    # Line-buffer the output: piped or run in a container, a block-buffered
    # stdout swallows the banner and the access log until the buffer fills.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):  # pragma: no cover - odd stdout
        pass
    config = Config(**{k: v for k, v in vars(args).items()})
    httpd = make_server(config)
    base = "http://%s:%d" % (args.host, args.port)
    print("mock-sap %s listening on %s  (client %s, db %s)"
          % (__version__, base, args.client, args.db_path))
    for svc in SERVICES.values():
        print("  OData  %s%s" % (base, svc.path))
    print("  RFC    %s/sap/bc/rfc/<FUNCTION>" % base)
    print("  IDoc   %s/sap/bc/idoc" % base)
    if args.oauth:
        print("  OAuth  %s/sap/bc/sec/oauth2/token  (client %s, tokens live %ds)"
              % (base, args.oauth.split(":", 1)[0], args.token_ttl))
    print("  Admin  %s/_mock/health" % base)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
