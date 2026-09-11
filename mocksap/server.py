"""The HTTP front end: routing, CSRF, authentication and fault injection."""
from __future__ import annotations

import base64
import datetime as _dt
import json
import random
import re
import secrets
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional
from urllib.parse import unquote, urlparse

from . import bapi, batch, db, idoc, messages, metadata, oauth
from .odata import SapError, error_payload
from .schema import SERVICES, service_for_path
from .service import JSON_CT, Context, Response, dispatch, parse_query

SYSTEM_ID = "MCK"
UNSAFE = {"POST", "PUT", "PATCH", "MERGE", "DELETE"}

SCENARIOS = {
    "slow": "Delays the response by the configured slow delay",
    "timeout": "Sleeps far longer than any sane client timeout",
    "error": "500 with a Gateway technical error",
    "busy": "503, as when all dialog work processes are busy",
    "auth": "401 with a NetWeaver Basic realm",
    "forbidden": "403, missing authorization object",
    "lock": "423, document locked by another user",
    "precondition": "412, as when the entity was changed after it was read",
    "expired-token": "401 invalid_token, as when a bearer token has run out",
    "notfound": "404 resource not found",
    "csrf": "403 CSRF token validation failed",
}


class Config:
    def __init__(self, **kw):
        self.host = kw.get("host", "127.0.0.1")
        self.port = kw.get("port", 8000)
        self.db_path = kw.get("db_path", ":memory:")
        self.client = kw.get("client", "100")
        self.user = kw.get("user", "MOCKUSER")
        self.basic_auth = kw.get("basic_auth")  # "user:password" or None
        self.oauth = kw.get("oauth")            # "client_id:client_secret" or None
        self.token_ttl = kw.get("token_ttl", 3600)
        self.csrf = kw.get("csrf", True)
        self.require_if_match = kw.get("require_if_match", False)
        self.latency_ms = kw.get("latency_ms", 0)
        self.error_rate = kw.get("error_rate", 0.0)
        self.slow_ms = kw.get("slow_ms", 3000)
        self.log_requests = kw.get("log_requests", True)
        self.quiet = kw.get("quiet", False)
        self.seed_value = kw.get("seed_value", 42)


class Faults:
    """Programmable fault rules, driven through /_mock/faults."""

    def __init__(self):
        self.rules: List[dict] = []
        self._lock = threading.Lock()
        self._next_id = 1

    def add(self, rule: dict) -> dict:
        with self._lock:
            rule = dict(rule)
            rule["id"] = self._next_id
            self._next_id += 1
            rule.setdefault("count", 0)  # 0 = unlimited
            rule.setdefault("hits", 0)
            self.rules.append(rule)
            return rule

    def clear(self) -> int:
        with self._lock:
            n = len(self.rules)
            self.rules = []
            return n

    def match(self, method: str, path: str) -> Optional[dict]:
        with self._lock:
            for rule in list(self.rules):
                if rule.get("method") and rule["method"].upper() != method:
                    continue
                pattern = rule.get("match", "")
                if pattern and not re.search(pattern, path):
                    continue
                rule["hits"] += 1
                if rule.get("count") and rule["hits"] >= rule["count"]:
                    self.rules.remove(rule)
                return rule
        return None


class MockSap:
    """Holds the shared state of a running mock system."""

    def __init__(self, config: Config):
        self.config = config
        self.conn = db.open_database(config.db_path)
        db.init_schema(self.conn)
        if db.counts(self.conn)["A_BusinessPartner"] == 0:
            db.seed(self.conn, config.seed_value)
        self.tokens = set()
        self.oauth = None
        if config.oauth:
            client_id, _, client_secret = config.oauth.partition(":")
            self.oauth = oauth.TokenStore(client_id, client_secret,
                                          config.token_ttl, config.user)
        self.faults = Faults()
        self.started = _dt.datetime.utcnow()
        self.rnd = random.Random(config.seed_value)

    def context(self, base_url: str, client: str, user: Optional[str] = None) -> Context:
        return Context(self.conn, base_url, user or self.config.user, client,
                       require_if_match=self.config.require_if_match)

    def close(self) -> None:
        """Release the database connection; used when a test shuts a mock down."""
        try:
            db.forget(self.conn)
            self.conn.close()
        except Exception:  # pragma: no cover - nothing useful to do here
            pass

    def new_token(self) -> str:
        token = secrets.token_urlsafe(18)
        self.tokens.add(token)
        return token


# --------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "SAP NetWeaver Application Server (mock)"
    sys_version = ""
    mock: MockSap = None  # injected by make_server

    # -- plumbing ---------------------------------------------------------
    def log_message(self, fmt, *args):  # pragma: no cover - console noise
        if not self.mock.config.quiet:
            print("%s - %s" % (self.address_string(), fmt % args))

    def do_GET(self):
        self._handle("GET")

    def do_HEAD(self):
        self._handle("HEAD")

    def do_POST(self):
        self._handle("POST")

    def do_PUT(self):
        self._handle("PUT")

    def do_PATCH(self):
        self._handle("PATCH")

    def do_MERGE(self):
        self._handle("MERGE")

    def do_DELETE(self):
        self._handle("DELETE")

    def do_OPTIONS(self):
        self._send(Response(200, headers={
            "Allow": "GET, HEAD, POST, PUT, PATCH, MERGE, DELETE, OPTIONS",
            "Access-Control-Allow-Methods": "GET, HEAD, POST, PUT, PATCH, MERGE, DELETE, OPTIONS",
            "Access-Control-Allow-Headers":
                "Authorization, Content-Type, Accept, X-CSRF-Token, sap-client, "
                "sap-mock-scenario, If-Match, DataServiceVersion, MaxDataServiceVersion",
            "Access-Control-Expose-Headers": "X-CSRF-Token, Location, DataServiceVersion",
        }, body=b"", content_type=None))

    # -- main entry -------------------------------------------------------
    def _handle(self, method: str):
        started = time.time()
        self._issue_token = None
        self._principal = None
        self._injected_message = None
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = parsed.query
        headers = {k.lower(): v for k, v in self.headers.items()}
        length = int(headers.get("content-length") or 0)
        body = self.rfile.read(length) if length else b""

        try:
            response = self._route(method, path, query, headers, body)
        except SapError as err:
            response = Response.error(err)
        except Exception as exc:  # pragma: no cover - defensive
            response = Response.error(SapError(
                "Unexpected mock failure: %s" % exc, 500,
                code="/IWBEP/CX_MGW_TECH_EXCEPTION"))

        if self._injected_message is not None and response.status < 400:
            injected = self._injected_message
            if isinstance(injected, str):
                injected = messages.message("MOCK/001", injected)
            response.headers.setdefault(
                "sap-message", messages.to_header([injected]) or "")

        if self._issue_token:
            response.headers["x-csrf-token"] = self._issue_token
            response.headers.setdefault(
                "Set-Cookie",
                "SAP_SESSIONID_%s_%s=%s; path=/"
                % (SYSTEM_ID, self.mock.config.client, uuid.uuid4().hex[:24]))

        duration = (time.time() - started) * 1000
        if self.mock.config.log_requests and not path.startswith("/_mock"):
            try:
                db.log_request(
                    self.mock.conn,
                    ts=_dt.datetime.utcnow().isoformat(), method=method, path=path,
                    query=query, headers=json.dumps(headers),
                    body=body[:8000].decode("utf-8", "replace"),
                    status=response.status, duration_ms=round(duration, 2))
            except Exception:
                pass
        self._send(response, head_only=(method == "HEAD"))

    def _send(self, response: Response, head_only: bool = False):
        body = b"" if head_only else response.body
        self.send_response(response.status)
        headers = dict(response.headers)
        headers.setdefault("Content-Length", str(len(response.body)))
        headers.setdefault("Access-Control-Allow-Origin", "*")
        headers.setdefault("sap-server", "true")
        headers.setdefault("sap-perf-fesrec", "%.6f" % (time.time() % 1000))
        headers.setdefault("sap-processing-info", "ODataBEP=,crp=,st=,MedCacheHub=,codeVersion=MOCK")
        for key, value in headers.items():
            if value is not None:
                self.send_header(key, value)
        self.end_headers()
        if body:
            try:
                self.wfile.write(body)
            except BrokenPipeError:  # pragma: no cover
                pass

    # -- routing ----------------------------------------------------------
    def _route(self, method, path, query, headers, body) -> Response:
        cfg = self.mock.config
        opts = parse_query(query)

        if path.startswith("/_mock"):
            return self._mock_api(method, path, opts, body)

        injected = self._inject_faults(method, path, headers, opts)
        if injected is not None:
            return injected

        # The OAuth endpoints sit in front of authentication - they are how a
        # client gets the credentials the rest of the surface demands - and in
        # front of CSRF, which a token request cannot have fetched yet.
        if path.startswith("/sap/bc/sec/oauth2/"):
            return self._oauth(method, path, headers, body)

        auth = self._check_auth(headers)
        if auth is not None:
            return auth

        client = opts.get("sap-client") or headers.get("sap-client") or cfg.client
        if client != cfg.client:
            raise SapError(
                "Client %s is not available in this system (mock client is %s)"
                % (client, cfg.client), 400, code="/IWFND/CM_CONSUMER")

        if cfg.latency_ms:
            time.sleep(cfg.latency_ms / 1000.0)
        if cfg.error_rate and self.mock.rnd.random() < cfg.error_rate:
            raise SapError(
                "Internal Server Error (injected by --error-rate)", 500,
                code="/IWBEP/CX_MGW_TECH_EXCEPTION")

        csrf = self._check_csrf(method, path, headers)
        if csrf is not None:
            return csrf

        base_url = self._base_url(headers)
        ctx = self.mock.context(base_url, client, self._principal)

        if path in ("/", "/index.html", "/sap", "/sap/"):
            return Response(body=_index_html(base_url), content_type="text/html;charset=utf-8")
        if path.rstrip("/").endswith("CATALOGSERVICE;v=2/ServiceCollection"):
            return self._catalog(base_url)

        if path.startswith("/sap/bc/rfc/"):
            return self._rfc_json(ctx, method, path, body)
        if path.startswith("/sap/bc/srt/"):
            return self._rfc_soap(ctx, method, path, body)
        if path.startswith("/sap/bc/idoc"):
            return self._idoc(ctx, method, path, opts, headers, body)

        svc, rest = service_for_path(path)
        if svc is not None:
            if (rest or "").strip("/") == "$batch":
                if method != "POST":
                    raise SapError("$batch requires POST", 405)
                content_type = headers.get("content-type", "")
                if svc.version >= 4 and "json" in content_type.lower():
                    return batch.handle_json_batch(ctx, body, svc.path)
                return batch.handle_batch(ctx, content_type, body, svc.path)
            return dispatch(ctx, method, path, query, headers, body)

        raise SapError("Resource not found for the segment '%s'" % path.strip("/"), 404)

    def _base_url(self, headers) -> str:
        host = headers.get("host") or "%s:%d" % (self.mock.config.host, self.mock.config.port)
        return "http://" + host

    # -- cross cutting ----------------------------------------------------
    def _check_auth(self, headers) -> Optional[Response]:
        """Basic, bearer, or both.  A request passes if it satisfies one of
        the mechanisms that are switched on; with none configured, the mock
        is open, as it has always been."""
        config = self.mock.config
        supplied = headers.get("authorization", "")
        store = self.mock.oauth

        if store is not None:
            token = oauth.bearer_token(supplied)
            if token is not None:
                try:
                    self._principal = store.validate(token).user
                    return None
                except oauth.OAuthError as err:
                    return self._bearer_challenge(err)
            if not config.basic_auth:
                return self._bearer_challenge(None)

        if config.basic_auth:
            if supplied.lower().startswith("basic "):
                try:
                    decoded = base64.b64decode(supplied.split(" ", 1)[1]).decode("utf-8")
                except Exception:
                    decoded = ""
                if decoded == config.basic_auth:
                    self._principal = config.basic_auth.split(":", 1)[0].upper()
                    return None
            return Response(
                401,
                headers={"WWW-Authenticate":
                         'Basic realm="SAP NetWeaver Application Server [%s/%s]"'
                         % (SYSTEM_ID, config.client)},
                body=error_payload(SapError("Authentication failed", 401)),
            )
        return None

    def _bearer_challenge(self, err) -> Response:
        """RFC 6750: name the problem in WWW-Authenticate, or just the realm
        when no credentials were presented at all."""
        realm = 'Bearer realm="SAP NetWeaver Application Server [%s/%s]"' % (
            SYSTEM_ID, self.mock.config.client)
        if err is None:
            message = "No bearer token was presented"
        else:
            realm += ', error="%s", error_description="%s"' % (err.error, err.description)
            message = err.description or err.error
        return Response(401, headers={"WWW-Authenticate": realm},
                        body=error_payload(SapError(message, 401)))

    def _check_csrf(self, method, path, headers) -> Optional[Response]:
        if not self.mock.config.csrf or not path.startswith("/sap/"):
            return None
        token_header = headers.get("x-csrf-token", "")
        if method in ("GET", "HEAD") and token_header.lower() == "fetch":
            self._issue_token = self.mock.new_token()
            return None
        if method in UNSAFE and token_header not in self.mock.tokens:
            return Response(
                403,
                headers={"x-csrf-token": "Required"},
                body=error_payload(SapError(
                    "CSRF token validation failed. Fetch a token with "
                    "'X-CSRF-Token: Fetch' on a GET request first.", 403,
                    code="/IWFND/CM_CSRF")),
            )
        return None

    def _inject_faults(self, method, path, headers, opts) -> Optional[Response]:
        scenario = (headers.get("sap-mock-scenario") or opts.get("sap-mock-scenario") or "").lower()
        rule = self.mock.faults.match(method, path)
        if rule and not scenario:
            scenario = (rule.get("scenario") or "").lower()
        if rule and rule.get("message") and not rule.get("status"):
            # a rule may warn without failing: remember it for the response
            self._injected_message = rule["message"]
            return None
        if rule and rule.get("status"):
            message = rule.get("message") or "Fault injected by mock rule %s" % rule["id"]
            if rule.get("delay_ms"):
                time.sleep(rule["delay_ms"] / 1000.0)
            return Response.error(SapError(message, int(rule["status"])))
        if not scenario:
            return None
        cfg = self.mock.config
        if scenario == "slow":
            time.sleep(cfg.slow_ms / 1000.0)
            return None
        if scenario == "timeout":
            time.sleep(120)
            return None
        if scenario == "error":
            return Response.error(SapError(
                "An exception was raised in the mock backend", 500,
                code="/IWBEP/CX_MGW_TECH_EXCEPTION"))
        if scenario == "busy":
            return Response(503, headers={"Retry-After": "30"}, body=error_payload(
                SapError("No dialog work process available", 503,
                         code="/IWFND/CM_MGW_RT")))
        if scenario == "auth":
            return Response(401, headers={
                "WWW-Authenticate": 'Basic realm="SAP NetWeaver Application Server [%s/%s]"'
                                    % (SYSTEM_ID, cfg.client)},
                body=error_payload(SapError("Authentication failed", 401)))
        if scenario == "forbidden":
            return Response.error(SapError(
                "No authorization to access Service '%s'" % path, 403,
                code="/IWFND/CM_BEC"))
        if scenario == "precondition":
            return Response(412, body=error_payload(SapError(
                "The entity was changed by another user after it was read",
                412, code="/IWBEP/CX_MGW_BUSI_EXCEPTION")))
        if scenario == "expired-token":
            return Response(401, headers={
                "WWW-Authenticate": 'Bearer realm="SAP NetWeaver Application Server '
                                    '[%s/%s]", error="invalid_token", '
                                    'error_description="The access token expired"'
                                    % (SYSTEM_ID, cfg.client)},
                body=error_payload(SapError("The access token expired", 401)))
        if scenario == "lock":
            return Response(423, body=error_payload(SapError(
                "Document is locked by user MOCKUSER", 423,
                code="/IWBEP/CX_MGW_BUSI_EXCEPTION")))
        if scenario == "notfound":
            return Response.error(SapError(
                "Resource not found for the segment '%s'" % path.strip("/"), 404))
        if scenario == "csrf":
            return Response(403, headers={"x-csrf-token": "Required"},
                            body=error_payload(SapError(
                                "CSRF token validation failed", 403, code="/IWFND/CM_CSRF")))
        raise SapError("Unknown mock scenario '%s'. Known: %s"
                       % (scenario, ", ".join(sorted(SCENARIOS))), 400)

    # -- endpoints --------------------------------------------------------
    def _catalog(self, base_url) -> Response:
        results = []
        for svc in SERVICES.values():
            uri = "%s/sap/opu/odata/IWFND/CATALOGSERVICE;v=2/ServiceCollection('%s_0001')" % (
                base_url, svc.name)
            results.append({
                "__metadata": {"id": uri, "uri": uri,
                               "type": "CATALOGSERVICE.Service"},
                "ID": "%s_0001" % svc.name,
                "Description": svc.title,
                "Title": svc.title,
                "TechnicalServiceVersion": 1,
                "TechnicalServiceName": svc.name,
                "ServiceUrl": base_url + svc.path,
                "MetadataUrl": base_url + svc.path + "/$metadata",
                "IsSapService": True,
                "UpdatedDate": "/Date(%d)/" % int(time.time() * 1000),
            })
        return Response(body={"d": {"results": results, "__count": str(len(results))}})

    def _oauth(self, method, path, headers, body) -> Response:
        store = self.mock.oauth
        rest = path[len("/sap/bc/sec/oauth2/"):].strip("/")
        if store is None:
            raise SapError(
                "OAuth is not switched on in this mock; start it with "
                "--oauth CLIENT_ID:CLIENT_SECRET", 404)
        if method != "POST":
            raise SapError("The OAuth endpoints require POST", 405)
        try:
            form = oauth.parse_form(body)
            basic = oauth.basic_credentials(headers.get("authorization", ""))
            if rest == "token":
                token = store.grant(form, basic)
                return Response(body=token.response(), headers={
                    "Cache-Control": "no-store", "Pragma": "no-cache"})
            if rest == "revoke":
                store.revoke(form.get("token", ""))
                return Response(200, body={}, content_type=None)  # RFC 7009: always 200
        except oauth.OAuthError as err:
            headers_out = {"Cache-Control": "no-store"}
            if err.status == 401:
                headers_out["WWW-Authenticate"] = 'Basic realm="oauth2"'
            return Response(err.status, body=err.payload(), headers=headers_out)
        raise SapError("Unknown OAuth endpoint '%s'" % rest, 404)

    def _rfc_json(self, ctx, method, path, body) -> Response:
        if method != "POST":
            raise SapError("RFC calls must use POST", 405)
        name = path[len("/sap/bc/rfc/"):].strip("/")
        if not name:
            return Response(body={"functions": sorted(bapi.FUNCTIONS)})
        try:
            params = json.loads(body.decode("utf-8")) if body.strip() else {}
        except (ValueError, UnicodeDecodeError) as exc:
            raise SapError("The request body is not valid JSON: %s" % exc, 400)
        if not isinstance(params, dict):
            raise SapError("The request body must be a JSON object of import parameters", 400)
        result = bapi.call(ctx, name, params, "json")
        return Response(body=result)

    def _rfc_soap(self, ctx, method, path, body) -> Response:
        if method != "POST":
            raise SapError("SOAP calls must use POST", 405)
        try:
            name, params = bapi.parse_soap(body)
            resolved = bapi.resolve(name)
            if resolved is None:
                raise SapError("Function module %s does not exist" % name, 500,
                               code="RFC_ERROR_FUNCTION_NOT_FOUND")
            result = bapi.call(ctx, resolved, params, "soap")
        except SapError as err:
            return Response(500 if err.status >= 500 else err.status,
                            body=bapi.soap_fault(err),
                            content_type='text/xml; charset=utf-8')
        return Response(body=bapi.soap_response(resolved, result),
                        content_type='text/xml; charset=utf-8')

    def _idoc(self, ctx, method, path, opts, headers, body) -> Response:
        rest = path[len("/sap/bc/idoc"):].strip("/")
        accept = (headers.get("accept", "") or "").lower()
        if "json" in accept:
            wants_xml = False          # an explicit Accept always wins
        elif "xml" in accept:
            wants_xml = True
        else:                          # otherwise answer in the dialect we were sent
            wants_xml = "xml" in (headers.get("content-type", "") or "").lower()
        if rest in ("", "idoc_xml"):
            if method == "POST":
                receipt = idoc.receive(ctx, headers.get("content-type", ""), body)
                if wants_xml:
                    return Response(201, body=idoc.receipt_xml(receipt),
                                    content_type="text/xml;charset=utf-8")
                return Response(201, body=receipt)
            if method == "GET":
                return Response(body={"results": idoc.listing(
                    ctx, int(opts.get("limit", 50)), opts.get("mestyp", ""))})
            raise SapError("Method %s is not allowed on the IDoc endpoint" % method, 405)
        if rest == "generate":
            if method != "POST":
                raise SapError("IDoc generation requires POST", 405)
            payload = json.loads(body.decode("utf-8")) if body.strip() else {}
            order = str(payload.get("SalesOrder") or opts.get("salesorder") or "")
            if not order:
                raise SapError("Provide a SalesOrder to generate an IDoc from", 400)
            mestyp = str(payload.get("mestyp") or payload.get("MESTYP")
                         or opts.get("mestyp") or "ORDERS")
            out = idoc.generate(ctx, mestyp, order)
            if wants_xml or opts.get("format") == "xml":
                return Response(201, body=out["xml"], content_type="text/xml;charset=utf-8",
                                headers={"sap-idoc-docnum": out["docnum"]})
            return Response(201, body=out)
        segments = rest.split("/")
        docnum = segments[0]
        if len(segments) == 2 and segments[1] == "status":
            if method in ("PUT", "POST", "PATCH"):
                payload = json.loads(body.decode("utf-8")) if body.strip() else {}
                return Response(body=idoc.set_status(
                    ctx, docnum, str(payload.get("status", "53")), payload.get("text", "")))
            raise SapError("Use PUT to set an IDoc status", 405)
        record = idoc.get(ctx, docnum)
        if record is None:
            raise SapError("IDoc %s does not exist" % docnum, 404)
        if opts.get("format") == "xml" or wants_xml:
            return Response(body=record["payload"], content_type="text/xml;charset=utf-8")
        return Response(body=record)

    # -- mock control plane ------------------------------------------------
    def _mock_api(self, method, path, opts, body) -> Response:
        mock = self.mock
        rest = path[len("/_mock"):].strip("/")
        payload: Dict[str, Any] = {}
        if body.strip():
            try:
                payload = json.loads(body.decode("utf-8"))
            except ValueError:
                raise SapError("The request body is not valid JSON", 400)

        if rest in ("", "health"):
            return Response(body={
                "status": "UP",
                "system": SYSTEM_ID,
                "client": mock.config.client,
                "database": mock.config.db_path,
                "csrf": mock.config.csrf,
                "requireIfMatch": mock.config.require_if_match,
                "auth": bool(mock.config.basic_auth),
                "oauth": bool(mock.oauth),
                "started": mock.started.isoformat() + "Z",
                "uptime_s": round((_dt.datetime.utcnow() - mock.started).total_seconds(), 1),
            })
        if rest == "state":
            return Response(body=db.counts(mock.conn))
        if rest == "services":
            base = self._base_url({k.lower(): v for k, v in self.headers.items()})
            return Response(body={"services": [{
                "name": s.name, "title": s.title, "odataVersion": s.version,
                "url": base + s.path,
                "metadata": base + s.path + "/$metadata",
                "entitySets": list(s.sets),
            } for s in SERVICES.values()],
                "rfc": sorted(bapi.FUNCTIONS),
                "scenarios": SCENARIOS})
        if rest == "reset":
            if method != "POST":
                raise SapError("Use POST to reset the mock", 405)
            db.reset(mock.conn)
            counts = db.seed(
                mock.conn,
                int(payload.get("seed", mock.config.seed_value)),
                int(payload.get("orders", 25)), int(payload.get("purchaseOrders", 12)))
            mock.tokens.clear()
            mock.faults.clear()
            return Response(body={"reset": True, "counts": counts})
        if rest == "requests":
            limit = int(opts.get("limit", 25))
            rows = mock.conn.execute(
                "SELECT id,ts,method,path,query,status,duration_ms FROM request_log "
                "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            return Response(body={"results": [dict(r) for r in rows]})
        if rest == "rfc-log":
            rows = mock.conn.execute(
                "SELECT id,ts,function_name,protocol FROM rfc_log ORDER BY id DESC LIMIT ?",
                (int(opts.get("limit", 25)),)).fetchall()
            return Response(body={"results": [dict(r) for r in rows]})
        if rest == "idocs":
            ctx = mock.context(self._base_url({}), mock.config.client)
            return Response(body={"results": idoc.listing(
                ctx, int(opts.get("limit", 50)), opts.get("mestyp", ""))})
        if rest == "tokens":
            if mock.oauth is None:
                raise SapError("OAuth is not switched on in this mock", 404)
            if method == "GET":
                return Response(body={"results": mock.oauth.listing()})
            if method == "DELETE":
                return Response(body={"revoked": mock.oauth.revoke_all()})
            raise SapError("Method %s is not allowed on /_mock/tokens" % method, 405)
        if rest == "faults":
            if method == "GET":
                return Response(body={"results": mock.faults.rules,
                                      "scenarios": SCENARIOS})
            if method == "POST":
                return Response(201, body=mock.faults.add(payload))
            if method == "DELETE":
                return Response(body={"cleared": mock.faults.clear()})
            raise SapError("Method %s is not allowed on /_mock/faults" % method, 405)
        raise SapError("Unknown mock endpoint '%s'" % rest, 404)


def _index_html(base_url: str) -> str:
    rows = "".join(
        '<tr><td><code>%s</code></td><td>V%d</td><td>%s</td>'
        '<td><a href="%s%s/$metadata">$metadata</a></td></tr>'
        % (svc.name, svc.version, svc.title, base_url, svc.path)
        for svc in SERVICES.values())
    functions = ", ".join("<code>%s</code>" % f for f in sorted(bapi.FUNCTIONS))
    return """<!doctype html><meta charset="utf-8"><title>Mock SAP system %s</title>
<style>body{font:15px/1.5 system-ui,sans-serif;margin:2rem auto;max-width:52rem;padding:0 1rem}
table{border-collapse:collapse;width:100%%}td,th{border-bottom:1px solid #ddd;padding:.4rem;text-align:left}
code{background:#f3f4f6;padding:.1rem .3rem;border-radius:3px}</style>
<h1>Mock SAP system %s / client 100</h1>
<p>A black-box SAP endpoint: it speaks the shapes, it does not do the ERP.</p>
<h2>OData services</h2><table>
<tr><th>Service</th><th>OData</th><th>Title</th><th>Metadata</th></tr>%s</table>
<h2>Other endpoints</h2>
<ul>
<li><code>POST /sap/bc/rfc/&lt;FUNCTION&gt;</code> - BAPI over JSON: %s</li>
<li><code>POST /sap/bc/srt/rfc/sap/&lt;service&gt;</code> - the same functions over SOAP</li>
<li><code>POST /sap/bc/idoc</code> - inbound IDoc (XML or EDI_DC40 flat file)</li>
<li><code>POST /sap/bc/idoc/generate</code> - outbound ORDERS05 from a sales order</li>
<li><code>GET /sap/opu/odata/IWFND/CATALOGSERVICE;v=2/ServiceCollection</code> - service catalog</li>
<li><code>GET /sap/bc/idoc/&lt;DOCNUM&gt;</code> - read a stored IDoc,
    <code>PUT /sap/bc/idoc/&lt;DOCNUM&gt;/status</code> - set its status</li>
<li><code>GET /_mock/health</code>, <code>/_mock/state</code>, <code>/_mock/services</code>,
    <code>/_mock/requests</code>, <code>/_mock/rfc-log</code>, <code>/_mock/idocs</code>,
    <code>/_mock/faults</code>, <code>POST /_mock/reset</code></li>
</ul>""" % (SYSTEM_ID, SYSTEM_ID, rows, functions)


def make_server(config: Config):
    mock = MockSap(config)

    class BoundHandler(Handler):
        mock = None

    BoundHandler.mock = mock

    httpd = ThreadingHTTPServer((config.host, config.port), BoundHandler)
    httpd.daemon_threads = True
    httpd.mock = mock
    return httpd
