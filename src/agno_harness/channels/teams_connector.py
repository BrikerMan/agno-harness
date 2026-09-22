"""Bot Framework connector client used by :class:`TeamsChannel`.

Outbound replies go to the inbound activity's ``serviceUrl`` with a client-credentials
token. The host is allowlisted so a crafted activity cannot turn the bot into an SSRF client.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import time
from dataclasses import dataclass
from urllib.parse import quote, urlparse

import httpx

log = logging.getLogger("agno_harness.channels.teams")

BOT_FRAMEWORK_SCOPE = "https://api.botframework.com/.default"
MULTI_TENANT_AUTHORITY = "https://login.microsoftonline.com/botframework.com"

_EXACT_HOSTS = frozenset(
    {
        "botframework.com",
        "botframework.azure.us",
        "smba.trafficmanager.net",
    }
)
_HOST_SUFFIXES = (
    ".botframework.com",
    ".botframework.azure.us",
    ".smba.trafficmanager.net",
)


class TeamsChannelError(RuntimeError):
    """Base error for Teams connector, auth, and delivery."""


class TeamsCredentialsError(TeamsChannelError):
    """App ID or password is missing, or the token endpoint rejected them."""


class TeamsAuthError(TeamsChannelError):
    """Inbound Bot Framework JWT is missing or invalid."""

    status_code = 401


class TeamsKeyDiscoveryError(TeamsAuthError):
    """Bot Framework signing keys could not be loaded."""

    status_code = 503


class TeamsServiceUrlError(TeamsChannelError):
    """``serviceUrl`` is not an allowlisted Bot Framework host."""


class TeamsDeliveryError(TeamsChannelError):
    """The connector could not deliver an activity."""


def teams_token_authority(tenant_id: str | None) -> str:
    """OAuth authority for the bot's client-credentials token.

    A set tenant is single-tenant (``login.microsoftonline.com/{tenant}``).
    An empty tenant is multi-tenant and uses the Bot Framework authority
    ``login.microsoftonline.com/botframework.com``.
    """
    tenant = (tenant_id or "").strip()
    if not tenant:
        return MULTI_TENANT_AUTHORITY
    if any(char in tenant for char in "/\\?#@"):
        raise TeamsCredentialsError("tenant_id contains unsupported characters")
    return f"https://login.microsoftonline.com/{tenant}"


def _host_allowed(host: str) -> bool:
    if host in _EXACT_HOSTS:
        return True
    return any(host.endswith(suffix) for suffix in _HOST_SUFFIXES)


def validate_service_url(service_url: str) -> str:
    """Return a normalized ``serviceUrl`` or raise :class:`TeamsServiceUrlError`.

    Allowed hosts are ``*.botframework.com``, ``*.botframework.azure.us``, and
    ``*.smba.trafficmanager.net`` (plus those apex names). The regional path
    (``/amer``, ``/emea``, …) is preserved. Query, fragment, userinfo, and
    non-443 ports are rejected.
    """
    raw = (service_url or "").strip()
    if not raw or any(char in raw for char in "\r\n\t\\"):
        raise TeamsServiceUrlError("serviceUrl is missing or malformed")
    parsed = urlparse(raw)
    if parsed.scheme != "https":
        raise TeamsServiceUrlError("serviceUrl must use https")
    if parsed.username or parsed.password or "@" in parsed.netloc:
        raise TeamsServiceUrlError("serviceUrl must not include userinfo")
    if parsed.query or parsed.fragment:
        raise TeamsServiceUrlError("serviceUrl must not include a query or fragment")
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host or host in {"localhost", "localhost.localdomain"}:
        raise TeamsServiceUrlError("serviceUrl host is not allowed")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise TeamsServiceUrlError("serviceUrl must be a Bot Framework hostname, not an IP")
    if parsed.port not in (None, 443):
        raise TeamsServiceUrlError("serviceUrl must use port 443")
    if not _host_allowed(host):
        raise TeamsServiceUrlError(f"serviceUrl host {host!r} is not an allowed Bot Framework host")
    path = parsed.path.rstrip("/")
    return f"https://{host}{path}"


@dataclass(frozen=True)
class TeamsConversationRef:
    """Conversation reference cached from an inbound activity so ``send`` can reply."""

    service_url: str
    conversation_id: str
    activity_id: str | None
    bot_id: str | None
    bot_name: str | None
    user_id: str | None
    user_name: str | None


def _response_activity_id(response: httpx.Response, body: object) -> str | None:
    """Read a Bot Connector activity id from the JSON body or Location header."""
    if isinstance(body, dict):
        raw = body.get("id") or body.get("Id")
        if raw:
            return str(raw)
    location = response.headers.get("Location") or ""
    tail = location.rstrip("/").rsplit("/", 1)[-1].strip()
    if tail and tail.lower() != "activities":
        return tail
    return None


class TeamsConnectorClient:
    """Minimal Bot Connector client: client-credentials token plus ``post activity``."""

    def __init__(
        self,
        app_id: str,
        app_password: str,
        tenant_id: str | None = None,
        *,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self.app_id = app_id
        self.app_password = app_password
        self.tenant_id = (tenant_id or "").strip() or None
        self.authority = teams_token_authority(self.tenant_id)
        self._http = http
        self._owns_http = http is None
        self._token: str | None = None
        self._token_deadline = 0.0
        self._token_lock = asyncio.Lock()

    async def aclose(self) -> None:
        if self._owns_http and self._http is not None:
            await self._http.aclose()
            self._http = None

    async def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=20)
        return self._http

    async def get_token(self) -> str:
        """Return a cached Bot Connector access token, refreshing it when needed."""
        if not self.app_id or not self.app_password:
            raise TeamsCredentialsError(
                "Teams app id and password are required. "
                "Set AGNO_HARNESS_TEAMS_APP_ID and AGNO_HARNESS_TEAMS_APP_PASSWORD."
            )
        now = time.monotonic()
        if self._token and now < self._token_deadline:
            return self._token
        async with self._token_lock:
            now = time.monotonic()
            if self._token and now < self._token_deadline:
                return self._token
            client = await self._client()
            url = f"{self.authority}/oauth2/v2.0/token"
            try:
                response = await client.post(
                    url,
                    data={
                        "grant_type": "client_credentials",
                        "client_id": self.app_id,
                        "client_secret": self.app_password,
                        "scope": BOT_FRAMEWORK_SCOPE,
                    },
                )
            except httpx.HTTPError as exc:
                raise TeamsCredentialsError(f"token endpoint request failed: {exc}") from exc
            if response.status_code >= 400:
                detail = response.text[:300]
                raise TeamsCredentialsError(
                    f"token endpoint returned HTTP {response.status_code}: {detail}"
                )
            payload = response.json()
            token = payload.get("access_token") if isinstance(payload, dict) else None
            if not isinstance(token, str) or not token:
                raise TeamsCredentialsError("token endpoint returned no access_token")
            expires_in = 3600
            if isinstance(payload, dict) and payload.get("expires_in") is not None:
                try:
                    expires_in = int(payload["expires_in"])
                except (TypeError, ValueError):
                    expires_in = 3600
            self._token = token
            self._token_deadline = time.monotonic() + max(expires_in - 60, 30)
            return token

    async def post_activity(
        self,
        ref: TeamsConversationRef,
        activity: dict[str, object],
        *,
        reply: bool,
    ) -> str:
        """POST an activity. Returns the connector activity id or raises."""
        if not ref.conversation_id:
            raise TeamsDeliveryError("conversation id is missing; cannot deliver to Teams")
        base = validate_service_url(ref.service_url)
        url = f"{base}/v3/conversations/{quote(ref.conversation_id, safe='')}/activities"
        if reply and ref.activity_id:
            url = f"{url}/{quote(ref.activity_id, safe='')}"
        token = await self.get_token()
        client = await self._client()
        try:
            response = await client.post(
                url,
                json=activity,
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.HTTPError as exc:
            raise TeamsDeliveryError(f"Bot Connector request failed: {exc}") from exc
        if response.status_code >= 400:
            raise TeamsDeliveryError(
                f"Bot Connector returned HTTP {response.status_code}: {response.text[:300]}"
            )
        body: object
        try:
            body = response.json() if response.content else {}
        except ValueError:
            body = {}
        activity_id = _response_activity_id(response, body)
        if activity_id:
            return activity_id
        # A 2xx with no id still accepted the activity. Failing the turn here
        # marks a delivered reply as an error.
        log.warning(
            "Bot Connector accepted the activity without an id status=%s body=%s",
            response.status_code,
            (response.text or "")[:300],
        )
        return ""
