"""OAuth 2.0: the token endpoint, bearer validation, refresh and revocation."""
import base64
import time
import unittest
from urllib.parse import urlencode

from support import MockServerCase, SRV

TOKEN_URL = "/sap/bc/sec/oauth2/token"
REVOKE_URL = "/sap/bc/sec/oauth2/revoke"
SAML_GRANT = "urn:ietf:params:oauth:grant-type:saml2-bearer"


def assertion_for(name: str) -> str:
    xml = (
        '<Assertion xmlns="urn:oasis:names:tc:SAML:2.0:assertion">'
        "<Subject><NameID>%s</NameID></Subject></Assertion>" % name
    ).encode()
    return base64.b64encode(xml).decode()


class OAuthCase(MockServerCase):
    config_kwargs = {"oauth": "SAP_CLIENT:s3cret", "csrf": False}

    def token_request(self, form: dict, basic=("SAP_CLIENT", "s3cret"), url=TOKEN_URL):
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        if basic:
            credentials = base64.b64encode(("%s:%s" % basic).encode()).decode()
            headers["Authorization"] = "Basic " + credentials
        return self.request("POST", url, body=urlencode(form), headers=headers)

    def fetch_token(self, **extra):
        form = {"grant_type": "client_credentials"}
        form.update(extra)
        status, _, body = self.token_request(form)
        self.assertEqual(status, 200, body)
        return body

    def bearer(self, token):
        return {"Authorization": "Bearer " + token, "Content-Type": "application/json"}


class TestTokenEndpoint(OAuthCase):
    def test_token_endpoint_is_reachable_without_a_token(self):
        status, headers, body = self.token_request({"grant_type": "client_credentials"})
        self.assertEqual(status, 200)
        self.assertEqual(body["token_type"], "Bearer")
        self.assertEqual(body["expires_in"], 3600)
        self.assertTrue(body["access_token"])
        self.assertTrue(body["refresh_token"])
        self.assertTrue(body["scope"])
        self.assertEqual(headers.get("Cache-Control"), "no-store")

    def test_client_credentials_in_the_body_work_too(self):
        status, _, body = self.token_request(
            {"grant_type": "client_credentials", "client_id": "SAP_CLIENT",
             "client_secret": "s3cret"}, basic=None)
        self.assertEqual(status, 200)
        self.assertTrue(body["access_token"])

    def test_client_authentication_failures(self):
        status, _, body = self.token_request(
            {"grant_type": "client_credentials"}, basic=("SAP_CLIENT", "wrong"))
        self.assertEqual(status, 401)
        self.assertEqual(body["error"], "invalid_client")

        status, _, body = self.token_request({}, basic=None)
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "invalid_request")

        status, _, body = self.token_request({"grant_type": "magic"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "unsupported_grant_type")

    def test_password_and_saml_grants_carry_a_user(self):
        status, _, body = self.token_request(
            {"grant_type": "password", "username": "HANS.BECKER", "password": "x"})
        self.assertEqual(status, 200)
        _, _, tokens = self.get("/_mock/tokens")
        self.assertEqual(tokens["results"][0]["user"], "HANS.BECKER")

        status, _, body = self.token_request(
            {"grant_type": SAML_GRANT, "assertion": assertion_for("ANNA.MUELLER")})
        self.assertEqual(status, 200)

        # and the principal is who the document says created it
        status, _, created = self.request(
            "POST", SRV + "/A_SalesOrder", headers=self.bearer(body["access_token"]),
            body={"SalesOrderType": "OR", "SalesOrganization": "1710",
                  "SoldToParty": "1000001", "DistributionChannel": "10",
                  "OrganizationDivision": "00"})
        self.assertEqual(status, 201)
        self.assertEqual(created["d"]["CreatedByUser"], "ANNA.MUELLER")

    def test_unreadable_assertion_is_refused(self):
        status, _, body = self.token_request(
            {"grant_type": SAML_GRANT, "assertion": "not-a-saml-document"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "invalid_grant")

        status, _, body = self.token_request(
            {"grant_type": SAML_GRANT,
             "assertion": base64.b64encode(b"<Assertion/>").decode()})
        self.assertEqual(status, 400)
        self.assertIn("NameID", body["error_description"])

    def test_get_is_not_allowed(self):
        status, _, _ = self.get(TOKEN_URL)
        self.assertEqual(status, 405)


class TestBearerValidation(OAuthCase):
    def test_a_token_opens_the_whole_surface(self):
        token = self.fetch_token()["access_token"]
        headers = self.bearer(token)

        status, _, body = self.get(SRV + "/A_SalesOrder?$top=1&$format=json",
                                   headers=headers)
        self.assertEqual(status, 200)
        self.assertTrue(body["d"]["results"])

        status, _, rfc = self.request("POST", "/sap/bc/rfc/STFC_CONNECTION",
                                      body={"REQUTEXT": "hello"}, headers=headers)
        self.assertEqual(status, 200)
        self.assertEqual(rfc["ECHOTEXT"], "hello")

        status, _, _ = self.get(SRV + "/$metadata", headers=headers, raw=True)
        self.assertEqual(status, 200)

    def test_missing_token_is_a_bare_challenge(self):
        status, headers, body = self.get(SRV + "/A_SalesOrder?$format=json")
        self.assertEqual(status, 401)
        challenge = headers.get("WWW-Authenticate", "")
        self.assertTrue(challenge.startswith("Bearer realm="))
        self.assertNotIn("error=", challenge, "nothing was presented, so nothing is wrong yet")
        self.assertIn("error", body)

    def test_unknown_token_says_why(self):
        status, headers, _ = self.get(SRV + "/A_SalesOrder?$format=json",
                                      headers={"Authorization": "Bearer nonsense"})
        self.assertEqual(status, 401)
        challenge = headers.get("WWW-Authenticate", "")
        self.assertIn('error="invalid_token"', challenge)
        self.assertIn("error_description=", challenge)

    def test_malformed_authorization_header(self):
        for value in ("Bearer", "Basic abc", "Token xyz", ""):
            status, _, _ = self.get(SRV + "/A_SalesOrder?$format=json",
                                    headers={"Authorization": value} if value else {})
            self.assertEqual(status, 401, value)

    def test_revocation(self):
        token = self.fetch_token()["access_token"]
        status, _, _ = self.get(SRV + "/A_SalesOrder?$top=1&$format=json",
                                headers=self.bearer(token))
        self.assertEqual(status, 200)

        status, _, _ = self.token_request({"token": token}, url=REVOKE_URL)
        self.assertEqual(status, 200)
        status, _, _ = self.get(SRV + "/A_SalesOrder?$top=1&$format=json",
                                headers=self.bearer(token))
        self.assertEqual(status, 401)

        # RFC 7009: revoking something unknown is still a 200
        status, _, _ = self.token_request({"token": "never-existed"}, url=REVOKE_URL)
        self.assertEqual(status, 200)

    def test_expired_token_scenario(self):
        token = self.fetch_token()["access_token"]
        headers = self.bearer(token)
        headers["sap-mock-scenario"] = "expired-token"
        status, resp_headers, _ = self.get(SRV + "/A_SalesOrder?$format=json",
                                           headers=headers)
        self.assertEqual(status, 401)
        self.assertIn("expired", resp_headers.get("WWW-Authenticate", ""))

    def test_token_listing_and_bulk_revocation(self):
        self.fetch_token()
        _, _, body = self.get("/_mock/tokens")
        self.assertTrue(body["results"])
        record = body["results"][0]
        self.assertEqual(record["grant"], "client_credentials")
        self.assertTrue(record["access_token"].endswith("…"),
                        "the listing must not hand out whole tokens")
        self.assertFalse(record["expired"])

        status, _, body = self.request("DELETE", "/_mock/tokens")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(body["revoked"], 1)
        _, _, body = self.get("/_mock/tokens")
        self.assertEqual(body["results"], [])

    def test_health_reports_oauth(self):
        _, _, health = self.get("/_mock/health")
        self.assertTrue(health["oauth"])


class TestExpiryAndRefresh(MockServerCase):
    config_kwargs = {"oauth": "SAP_CLIENT:s3cret", "csrf": False, "token_ttl": 1}

    def test_expiry_then_refresh(self):
        form = urlencode({"grant_type": "client_credentials",
                          "client_id": "SAP_CLIENT", "client_secret": "s3cret"})
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        _, _, first = self.request("POST", TOKEN_URL, body=form, headers=headers)
        self.assertEqual(first["expires_in"], 1)

        bearer = {"Authorization": "Bearer " + first["access_token"]}
        status, _, _ = self.get(SRV + "/A_SalesOrder?$top=1&$format=json", headers=bearer)
        self.assertEqual(status, 200)

        time.sleep(1.2)
        status, resp_headers, _ = self.get(SRV + "/A_SalesOrder?$format=json",
                                           headers=bearer)
        self.assertEqual(status, 401)
        self.assertIn("expired", resp_headers.get("WWW-Authenticate", ""))

        refresh = urlencode({"grant_type": "refresh_token",
                             "refresh_token": first["refresh_token"],
                             "client_id": "SAP_CLIENT", "client_secret": "s3cret"})
        status, _, second = self.request("POST", TOKEN_URL, body=refresh, headers=headers)
        self.assertEqual(status, 200)
        self.assertNotEqual(second["access_token"], first["access_token"])

        status, _, _ = self.get(
            SRV + "/A_SalesOrder?$top=1&$format=json",
            headers={"Authorization": "Bearer " + second["access_token"]})
        self.assertEqual(status, 200)

        # the refresh token rotated: the old one is spent
        status, _, body = self.request("POST", TOKEN_URL, body=refresh, headers=headers)
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "invalid_grant")


class TestOAuthBesideBasic(MockServerCase):
    config_kwargs = {"oauth": "SAP_CLIENT:s3cret", "basic_auth": "sapuser:secret",
                     "csrf": False}

    def test_either_mechanism_is_accepted(self):
        basic = base64.b64encode(b"sapuser:secret").decode()
        status, _, _ = self.get(SRV + "/A_SalesOrder?$top=1&$format=json",
                                headers={"Authorization": "Basic " + basic})
        self.assertEqual(status, 200)

        form = urlencode({"grant_type": "client_credentials",
                          "client_id": "SAP_CLIENT", "client_secret": "s3cret"})
        _, _, token = self.request(
            "POST", TOKEN_URL, body=form,
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        status, _, _ = self.get(
            SRV + "/A_SalesOrder?$top=1&$format=json",
            headers={"Authorization": "Bearer " + token["access_token"]})
        self.assertEqual(status, 200)

        status, _, _ = self.get(SRV + "/A_SalesOrder?$format=json")
        self.assertEqual(status, 401)


class TestOAuthSwitchedOff(MockServerCase):
    def test_the_endpoint_explains_itself(self):
        status, _, body = self.request(
            "POST", TOKEN_URL, body="grant_type=client_credentials",
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(status, 404)
        self.assertIn("--oauth", body["error"]["message"]["value"])

    def test_tokens_endpoint_is_absent(self):
        status, _, _ = self.get("/_mock/tokens")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
