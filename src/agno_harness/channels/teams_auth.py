"""Bot Framework JWT verification for the Teams webhook.

Inbound calls must present a Bearer token signed with RS256 by the Bot Connector
(or the Bot Framework emulator). Audience is the bot's Microsoft App ID. A missing
or invalid token is rejected; the webhook does not accept anonymous POSTs.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from collections.abc import Mapping
from typing import Any

import httpx

from .teams_connector import TeamsAuthError, TeamsCredentialsError, TeamsKeyDiscoveryError

log = logging.getLogger("agno_harness.channels.teams")

BOT_FRAMEWORK_ISSUER = "https://api.botframework.com"
EMULATOR_ISSUERS = frozenset(
    {
        "https://sts.windows.net/d6d49420-f39b-4df7-a1dc-d59a935871db/",
        "https://login.microsoftonline.com/d6d49420-f39b-4df7-a1dc-d59a935871db/v2.0",
    }
)
ALLOWED_ISSUERS = frozenset({BOT_FRAMEWORK_ISSUER, *EMULATOR_ISSUERS})

_OPENID_DOCUMENTS = (
    "https://login.botframework.com/v1/.well-known/openidconfiguration",
    "https://login.microsoftonline.com/botframework.com/v2.0/.well-known/openid-configuration",
)
_JWKS_TTL_SECONDS = 3600.0
_CLOCK_SKEW_SECONDS = 300


def _import_rsa() -> Any:
    try:
        from Crypto.Hash import SHA256
        from Crypto.PublicKey import RSA
        from Crypto.Signature import pkcs1_15
    except ImportError as exc:
        raise TeamsCredentialsError(
            "Teams JWT verification requires PyCryptodome. "
            "Install with: uv add 'agno-harness[teams]'"
        ) from exc
    return RSA, SHA256, pkcs1_15


def _b64decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def _b64uint(data: str) -> int:
    return int.from_bytes(_b64decode(data), "big")


def rsa_key_from_jwk(jwk: Mapping[str, Any]) -> Any:
    """Build an RSA public key from a JWK ``n`` / ``e`` pair."""
    rsa_mod, _, _ = _import_rsa()
    n = jwk.get("n")
    e = jwk.get("e")
    if not isinstance(n, str) or not isinstance(e, str):
        raise TeamsAuthError("bearer token rejected")
    return rsa_mod.construct((_b64uint(n), _b64uint(e)))


def rs256_verify(key: Any, signing_input: bytes, signature: bytes) -> bool:
    """Return whether ``signature`` is a valid RS256 signature for ``signing_input``."""
    _, sha256, pkcs1_15 = _import_rsa()
    try:
        pkcs1_15.new(key).verify(sha256.new(signing_input), signature)
    except (ValueError, TypeError):
        return False
    return True


def parse_bearer(authorization: str | None) -> str:
    """Return the raw JWT or raise :class:`TeamsAuthError`."""
    if authorization is None or not authorization.strip():
        raise TeamsAuthError("missing bearer token")
    scheme, _, token = authorization.strip().partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise TeamsAuthError("missing bearer token")
    return token.strip()


class TeamsJwtVerifier:
    """Validate Bot Framework channel tokens against JWKS and ``audience=app id``."""

    def __init__(
        self,
        app_id: str,
        *,
        keys: Mapping[str, Any] | None = None,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self.app_id = app_id
        # ``keys`` switches the verifier to offline mode (tests, injected JWKS).
        # ``None`` loads the public Bot Framework documents.
        self._static_keys = dict(keys) if keys is not None else None
        self._http = http
        self._owns_http = http is None
        self._key_by_kid: dict[str, Any] = {}
        self._jwks_loaded_at = 0.0

    async def aclose(self) -> None:
        if self._owns_http and self._http is not None:
            await self._http.aclose()
            self._http = None

    async def verify_authorization(self, authorization: str | None) -> dict[str, Any]:
        """Verify ``Authorization`` and return the JWT claims."""
        if not self.app_id:
            raise TeamsCredentialsError(
                "AGNO_HARNESS_TEAMS_APP_ID is required to verify Bot Framework JWTs"
            )
        token = parse_bearer(authorization)
        try:
            header_b64, payload_b64, signature_b64 = token.split(".")
            header = json.loads(_b64decode(header_b64))
            claims = json.loads(_b64decode(payload_b64))
        except (ValueError, json.JSONDecodeError) as exc:
            raise TeamsAuthError("malformed bearer token") from exc
        if not isinstance(header, dict) or not isinstance(claims, dict):
            raise TeamsAuthError("malformed bearer token")
        if header.get("alg") != "RS256":
            raise TeamsAuthError("bearer token rejected")
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise TeamsAuthError("bearer token is missing kid")
        key = await self._signing_key(kid)
        if not rs256_verify(key, f"{header_b64}.{payload_b64}".encode(), _b64decode(signature_b64)):
            raise TeamsAuthError("bearer token rejected")
        self._check_claims(claims)
        return claims

    def _check_claims(self, claims: Mapping[str, Any]) -> None:
        now = time.time()
        exp = claims.get("exp")
        if isinstance(exp, bool) or not isinstance(exp, int | float):
            raise TeamsAuthError("bearer token rejected")
        if now > float(exp) + _CLOCK_SKEW_SECONDS:
            raise TeamsAuthError("bearer token rejected")
        nbf = claims.get("nbf")
        if (
            nbf is not None
            and not isinstance(nbf, bool)
            and isinstance(nbf, int | float)
            and now + _CLOCK_SKEW_SECONDS < float(nbf)
        ):
            raise TeamsAuthError("bearer token rejected")
        audience = claims.get("aud")
        audiences = audience if isinstance(audience, list) else [audience]
        if self.app_id not in audiences:
            raise TeamsAuthError("bearer token rejected")
        if claims.get("iss") not in ALLOWED_ISSUERS:
            raise TeamsAuthError("unexpected token issuer")

    async def _signing_key(self, kid: str) -> Any:
        if self._static_keys is not None:
            key = self._static_keys.get(kid)
            if key is None:
                raise TeamsAuthError("bearer token rejected")
            return key
        key = self._key_by_kid.get(kid)
        if key is None or (time.monotonic() - self._jwks_loaded_at) > _JWKS_TTL_SECONDS:
            await self._refresh_jwks()
            key = self._key_by_kid.get(kid)
        if key is None:
            raise TeamsAuthError("bearer token rejected")
        return key

    async def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=10)
        return self._http

    async def _refresh_jwks(self) -> None:
        client = await self._client()
        found: dict[str, Any] = {}
        for document_url in _OPENID_DOCUMENTS:
            try:
                meta_response = await client.get(document_url)
                meta_response.raise_for_status()
                meta = meta_response.json()
                jwks_uri = meta["jwks_uri"] if isinstance(meta, dict) else None
                if not isinstance(jwks_uri, str) or not jwks_uri:
                    continue
                jwks_response = await client.get(jwks_uri)
                jwks_response.raise_for_status()
                jwks = jwks_response.json()
            except (httpx.HTTPError, KeyError, ValueError) as exc:
                log.warning("Bot Framework JWKS fetch failed for %s: %s", document_url, exc)
                continue
            keys = jwks.get("keys", []) if isinstance(jwks, dict) else []
            for jwk in keys:
                if not isinstance(jwk, dict) or jwk.get("kty") not in (None, "RSA"):
                    continue
                kid = jwk.get("kid")
                if not isinstance(kid, str) or not kid or "n" not in jwk or "e" not in jwk:
                    continue
                try:
                    found[kid] = rsa_key_from_jwk(jwk)
                except (TeamsAuthError, ValueError, TypeError) as exc:
                    log.warning("Skipping Bot Framework JWK kid=%s: %s", kid, exc)
        if not found:
            raise TeamsKeyDiscoveryError("failed to load Bot Framework signing keys")
        self._key_by_kid = found
        self._jwks_loaded_at = time.monotonic()
