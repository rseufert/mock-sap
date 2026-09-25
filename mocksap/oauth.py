"""A mock OAuth 2.0 authorization server, the way S/4HANA Cloud fronts its APIs.

Nothing here is cryptography.  Tokens are opaque strings the mock remembers
in memory, and a SAML assertion is read for its NameID and otherwise
believed.  That is deliberate: the point is to let a client exercise the
flow it has to implement - fetch a token, send it as a bearer, notice a 401,
refresh, retry - not to stand in for a real authorization server.
"""
from __future__ import annotations

import base64
import binascii
import datetime as _dt
import secrets
import threading
from typing import Dict, List, Optional
from urllib.parse import parse_qsl
from xml.etree import ElementTree as ET

CLIENT_CREDENTIALS = "client_credentials"
PASSWORD = "password"
REFRESH_TOKEN = "refresh_token"
SAML2_BEARER = "urn:ietf:params:oauth:grant-type:saml2-bearer"

GRANTS = (CLIENT_CREDENTIALS, PASSWORD, REFRESH_TOKEN, SAML2_BEARER)

DEFAULT_SCOPE = "API_SALES_ORDER_SRV_0001 API_BUSINESS_PARTNER_SRV_0001"


class OAuthError(Exception):
    """An RFC 6749 error response."""

    def __init__(self, error: str, description: str = "", status: int = 400):
        super().__init__(description or error)
        self.error = error
        self.description = description
        self.status = status

    def payload(self) -> dict:
        body = {"error": self.error}
        if self.description:
            body["error_description"] = self.description
        return body


class Token:
    __slots__ = ("value", "refresh_value", "client_id", "user", "scope",
                 "issued_at", "expires_at", "grant")

    def __init__(self, value, refresh_value, client_id, user, scope, ttl, grant):
        now = _dt.datetime.utcnow()
        self.value = value
        self.refresh_value = refresh_value
        self.client_id = client_id
        self.user = user
        self.scope = scope
        self.grant = grant
        self.issued_at = now
        self.expires_at = now + _dt.timedelta(seconds=ttl)

    @property
    def expired(self) -> bool:
        return _dt.datetime.utcnow() >= self.expires_at

    @property
    def lifetime(self) -> int:
        """The seconds this token was issued for.

        RFC 6749 defines a token response's ``expires_in`` as the lifetime of
        the token, which is a property of the token and not of the clock: a
        freshly issued one reports the whole of it. Deriving it from
        ``issued_at`` reports that exactly, however long the server then spends
        writing the response - where reading the clock again shortens it, and
        a rounded value can be short by a whole second.
        """
        return max(0, int(round((self.expires_at - self.issued_at).total_seconds())))

    @property
    def expires_in(self) -> int:
        """The seconds this token has left, which is what a listing wants."""
        return max(0, int(round((self.expires_at - _dt.datetime.utcnow()).total_seconds())))

    def response(self) -> dict:
        return {
            "access_token": self.value,
            "token_type": "Bearer",
            "expires_in": self.lifetime,
            "scope": self.scope,
            "refresh_token": self.refresh_value,
        }

    def describe(self) -> dict:
        return {
            "access_token": self.value[:8] + "…",
            "client_id": self.client_id,
            "user": self.user,
            "scope": self.scope,
            "grant": self.grant,
            "issued_at": self.issued_at.isoformat() + "Z",
            "expires_at": self.expires_at.isoformat() + "Z",
            "expires_in": self.expires_in,
            "expired": self.expired,
        }


class TokenStore:
    """Issues and validates the opaque tokens."""

    def __init__(self, client_id: str, client_secret: str, ttl: int = 3600,
                 default_user: str = "MOCKUSER"):
        self.client_id = client_id
        self.client_secret = client_secret
        self.ttl = ttl
        self.default_user = default_user
        self._by_access: Dict[str, Token] = {}
        self._by_refresh: Dict[str, Token] = {}
        self._lock = threading.Lock()

    # -- issuing -----------------------------------------------------------
    def issue(self, grant: str, user: Optional[str] = None,
              scope: Optional[str] = None, ttl: Optional[int] = None) -> Token:
        token = Token(
            "sap-" + secrets.token_urlsafe(32),
            "sapr-" + secrets.token_urlsafe(32),
            self.client_id,
            user or self.default_user,
            scope or DEFAULT_SCOPE,
            self.ttl if ttl is None else ttl,
            grant,
        )
        with self._lock:
            self._by_access[token.value] = token
            self._by_refresh[token.refresh_value] = token
        return token

    def grant(self, form: Dict[str, str], basic: Optional[tuple]) -> Token:
        """Handle one token request."""
        grant_type = form.get("grant_type", "")
        if not grant_type:
            raise OAuthError("invalid_request", "The grant_type parameter is missing")
        if grant_type not in GRANTS:
            raise OAuthError(
                "unsupported_grant_type",
                "This mock supports the grant types: %s" % ", ".join(GRANTS))

        if grant_type == REFRESH_TOKEN:
            self._authenticate_client(form, basic)
            supplied = form.get("refresh_token", "")
            with self._lock:
                previous = self._by_refresh.get(supplied)
            if previous is None:
                raise OAuthError("invalid_grant", "The refresh token is not known")
            self.revoke(previous.value)
            return self.issue(REFRESH_TOKEN, previous.user, previous.scope)

        if grant_type == SAML2_BEARER:
            # The assertion is read, not verified: no signature checking here.
            user = _name_id(form.get("assertion", ""))
            self._authenticate_client(form, basic, required=False)
            return self.issue(SAML2_BEARER, user, form.get("scope"))

        self._authenticate_client(form, basic)
        if grant_type == PASSWORD:
            user = form.get("username") or self.default_user
            return self.issue(PASSWORD, user, form.get("scope"))
        return self.issue(CLIENT_CREDENTIALS, self.default_user, form.get("scope"))

    def _authenticate_client(self, form, basic, required: bool = True) -> None:
        client_id = form.get("client_id")
        client_secret = form.get("client_secret")
        if basic:
            client_id, client_secret = basic
        if client_id is None and client_secret is None and not required:
            return
        if client_id != self.client_id or client_secret != self.client_secret:
            raise OAuthError(
                "invalid_client", "Client authentication failed", status=401)

    # -- validating --------------------------------------------------------
    def validate(self, value: str) -> Token:
        with self._lock:
            token = self._by_access.get(value)
        if token is None:
            raise OAuthError(
                "invalid_token", "The access token is not known to this system",
                status=401)
        if token.expired:
            raise OAuthError(
                "invalid_token", "The access token expired at %sZ"
                % token.expires_at.isoformat(), status=401)
        return token

    def revoke(self, value: str) -> bool:
        with self._lock:
            token = self._by_access.pop(value, None)
            if token is None:
                token = self._by_refresh.get(value)
                if token is None:
                    return False
                self._by_access.pop(token.value, None)
            self._by_refresh.pop(token.refresh_value, None)
        return True

    def revoke_all(self) -> int:
        with self._lock:
            count = len(self._by_access)
            self._by_access.clear()
            self._by_refresh.clear()
        return count

    def listing(self) -> List[dict]:
        with self._lock:
            tokens = list(self._by_access.values())
        return [t.describe() for t in sorted(tokens, key=lambda t: t.issued_at, reverse=True)]


def parse_form(body: bytes) -> Dict[str, str]:
    try:
        return dict(parse_qsl(body.decode("utf-8"), keep_blank_values=True))
    except UnicodeDecodeError:
        raise OAuthError("invalid_request", "The request body is not valid form data")


def basic_credentials(header: str) -> Optional[tuple]:
    """RFC 6749 prefers the client credentials in an Authorization header."""
    if not header or not header.lower().startswith("basic "):
        return None
    try:
        decoded = base64.b64decode(header.split(" ", 1)[1]).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, IndexError):
        raise OAuthError("invalid_client", "Malformed Basic credentials", status=401)
    client_id, _, client_secret = decoded.partition(":")
    return client_id, client_secret


def bearer_token(header: str) -> Optional[str]:
    if not header or not header.lower().startswith("bearer "):
        return None
    return header.split(" ", 1)[1].strip()


def _name_id(assertion: str) -> Optional[str]:
    """Read the NameID out of a SAML assertion.  No signature is checked."""
    if not assertion:
        raise OAuthError("invalid_grant", "The assertion parameter is missing")
    raw = assertion.strip()
    for decode in (lambda v: base64.b64decode(v + "=" * (-len(v) % 4)),
                   lambda v: v.encode("utf-8")):
        try:
            xml = decode(raw)
            root = ET.fromstring(xml)
        except (binascii.Error, ValueError, ET.ParseError):
            continue
        for element in root.iter():
            if element.tag.split("}")[-1] == "NameID" and (element.text or "").strip():
                return element.text.strip()
        raise OAuthError("invalid_grant", "The assertion carries no NameID")
    raise OAuthError("invalid_grant", "The assertion is not a readable SAML document")
