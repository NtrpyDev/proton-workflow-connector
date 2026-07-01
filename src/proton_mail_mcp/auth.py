from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import urlparse

from mcp.server.auth.provider import AccessToken


class OIDCTokenVerifier:
    def __init__(self, *, issuer_url: str, audience: str, jwks_url: str = "") -> None:
        self.issuer_url = issuer_url.rstrip("/")
        self.audience = audience
        self.jwks_url = jwks_url
        self._jwks_client: Any = None

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            return await asyncio.to_thread(self._verify_token, token)
        except Exception:
            return None

    def _verify_token(self, token: str) -> AccessToken:
        import jwt

        if self._jwks_client is None:
            self._jwks_client = jwt.PyJWKClient(self.jwks_url or self._discover_jwks_url())
        signing_key = self._jwks_client.get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256", "RS384", "RS512", "ES256", "ES384", "ES512", "EdDSA"],
            audience=self.audience,
            issuer=self.issuer_url,
            options={"require": ["exp", "iss", "sub"]},
        )
        scopes = _token_scopes(claims)
        client_id = str(claims.get("client_id") or claims.get("azp") or claims["sub"])
        return AccessToken(
            token=token,
            client_id=client_id,
            scopes=scopes,
            expires_at=int(claims["exp"]),
            resource=self.audience,
            subject=str(claims["sub"]),
            claims={"iss": claims.get("iss"), "azp": claims.get("azp")},
        )

    def _discover_jwks_url(self) -> str:
        import httpx

        url = f"{self.issuer_url}/.well-known/openid-configuration"
        response = httpx.get(url, timeout=10.0)
        response.raise_for_status()
        data = response.json()
        jwks_uri = data.get("jwks_uri")
        if not isinstance(jwks_uri, str) or not jwks_uri:
            raise RuntimeError("OIDC discovery response did not include jwks_uri")
        parsed = urlparse(jwks_uri)
        if parsed.hostname not in {"127.0.0.1", "localhost", "::1"} and parsed.scheme != "https":
            raise RuntimeError("OIDC discovery returned a non-HTTPS JWKS URL")
        return jwks_uri


def _token_scopes(claims: dict[str, Any]) -> list[str]:
    value = claims.get("scope", claims.get("scp", []))
    if isinstance(value, str):
        return value.split()
    if isinstance(value, list):
        return [str(item) for item in value]
    return []
