import asyncio
import time
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from proton_mail_mcp.auth import OIDCTokenVerifier


def test_oidc_verifier_accepts_valid_token_and_extracts_scopes():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = jwt.encode(
        {
            "iss": "https://issuer.example.com",
            "aud": "https://mail.example.com/mcp",
            "sub": "user-1",
            "azp": "client-1",
            "exp": int(time.time()) + 300,
            "scope": "proton-workflow-connector mail.read",
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "test-key"},
    )
    verifier = OIDCTokenVerifier(
        issuer_url="https://issuer.example.com",
        audience="https://mail.example.com/mcp",
    )
    verifier._jwks_client = SimpleNamespace(
        get_signing_key_from_jwt=lambda _token: SimpleNamespace(key=private_key.public_key())
    )

    access_token = asyncio.run(verifier.verify_token(token))

    assert access_token is not None
    assert access_token.client_id == "client-1"
    assert access_token.subject == "user-1"
    assert access_token.scopes == ["proton-workflow-connector", "mail.read"]


def test_oidc_verifier_rejects_wrong_audience():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = jwt.encode(
        {
            "iss": "https://issuer.example.com",
            "aud": "https://wrong.example.com/mcp",
            "sub": "user-1",
            "exp": int(time.time()) + 300,
        },
        private_key,
        algorithm="RS256",
    )
    verifier = OIDCTokenVerifier(
        issuer_url="https://issuer.example.com",
        audience="https://mail.example.com/mcp",
    )
    verifier._jwks_client = SimpleNamespace(
        get_signing_key_from_jwt=lambda _token: SimpleNamespace(key=private_key.public_key())
    )

    assert asyncio.run(verifier.verify_token(token)) is None


@pytest.mark.parametrize(
    ("claim_overrides", "signing_key"),
    [
        ({"exp": int(time.time()) - 1}, None),
        ({"iss": "https://wrong-issuer.example.com"}, None),
        ({}, rsa.generate_private_key(public_exponent=65537, key_size=2048)),
    ],
)
def test_oidc_verifier_rejects_expired_wrong_issuer_and_bad_signature(claim_overrides, signing_key):
    trusted_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token_key = signing_key or trusted_key
    claims = {
        "iss": "https://issuer.example.com",
        "aud": "https://mail.example.com/mcp",
        "sub": "user-1",
        "exp": int(time.time()) + 300,
    }
    claims.update(claim_overrides)
    token = jwt.encode(claims, token_key, algorithm="RS256")
    verifier = OIDCTokenVerifier(
        issuer_url="https://issuer.example.com",
        audience="https://mail.example.com/mcp",
    )
    verifier._jwks_client = SimpleNamespace(
        get_signing_key_from_jwt=lambda _token: SimpleNamespace(key=trusted_key.public_key())
    )

    assert asyncio.run(verifier.verify_token(token)) is None


def test_oidc_verifier_accepts_list_scope_claim():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = jwt.encode(
        {
            "iss": "https://issuer.example.com",
            "aud": "https://mail.example.com/mcp",
            "sub": "user-1",
            "exp": int(time.time()) + 300,
            "scp": ["proton-workflow-connector", "mail.read"],
        },
        private_key,
        algorithm="RS256",
    )
    verifier = OIDCTokenVerifier(
        issuer_url="https://issuer.example.com",
        audience="https://mail.example.com/mcp",
    )
    verifier._jwks_client = SimpleNamespace(
        get_signing_key_from_jwt=lambda _token: SimpleNamespace(key=private_key.public_key())
    )

    access_token = asyncio.run(verifier.verify_token(token))

    assert access_token is not None
    assert access_token.scopes == ["proton-workflow-connector", "mail.read"]


def test_jwks_fetches_send_an_explicit_user_agent(monkeypatch):
    # CDNs/WAFs in front of an issuer block urllib's default UA; a silent 403 there
    # rejects every token, so both the discovery and JWKS fetches must identify themselves.
    seen: dict[str, dict] = {}

    def fake_get(url, **kwargs):
        seen["discovery"] = kwargs.get("headers") or {}
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"jwks_uri": "https://issuer.example.com/jwks"},
        )

    class FakeJWKClient:
        def __init__(self, url, headers=None, **kwargs):
            seen["jwks"] = headers or {}

    monkeypatch.setattr("httpx.get", fake_get)
    monkeypatch.setattr(jwt, "PyJWKClient", FakeJWKClient)
    verifier = OIDCTokenVerifier(
        issuer_url="https://issuer.example.com",
        audience="https://mail.example.com/mcp",
    )

    asyncio.run(verifier.verify_token("not-a-jwt"))

    assert seen["discovery"].get("User-Agent") == "proton-workflow-connector"
    assert seen["jwks"].get("User-Agent") == "proton-workflow-connector"


def test_oidc_discovery_rejects_insecure_remote_jwks(monkeypatch):
    response = SimpleNamespace(
        raise_for_status=lambda: None,
        json=lambda: {"jwks_uri": "http://issuer.example.com/jwks"},
    )
    monkeypatch.setattr("httpx.get", lambda *args, **kwargs: response)
    verifier = OIDCTokenVerifier(
        issuer_url="https://issuer.example.com",
        audience="https://mail.example.com/mcp",
    )

    with pytest.raises(RuntimeError, match="non-HTTPS JWKS"):
        verifier._discover_jwks_url()
