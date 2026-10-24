"""Microsoft Graph helper for Teams channel.

Handles:
1. Microsoft Graph token acquisition (using Bot Framework app credentials or MS_GRAPH_* overrides).
2. Fetching real message attachments from Microsoft Graph for chat and channel messages
   (since Bot Framework webhook activities do not include reference attachment payloads).
3. Downloading file contents from SharePoint / OneDrive sharing URLs via Graph Shares API.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
import time
from html import unescape
from typing import Any
from urllib.parse import quote

import httpx

from ...core.attachment import InboundAttachment

log = logging.getLogger("agno_harness.channels.teams_graph")

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
LOGIN_BASE = "https://login.microsoftonline.com"
GRAPH_SCOPE = "https://graph.microsoft.com/.default"

_DOC_EXTENSIONS = (
    ".docx",
    ".doc",
    ".pdf",
    ".txt",
    ".md",
    ".csv",
    ".xlsx",
    ".xls",
    ".pptx",
    ".ppt",
    ".json",
)

_FILE_LINK_RE = re.compile(
    r'<a\s+[^>]*href=["\'](?P<url>https?://[^"\']+)["\'][^>]*>(?P<name>.*?)</a>',
    flags=re.IGNORECASE | re.DOTALL,
)

_CARD_TYPES = ("application/vnd.microsoft.card", "application/vnd.microsoft.teams")


def encode_sharing_url(url: str) -> str:
    """Encode a SharePoint/OneDrive sharing URL for Graph ``/shares/{token}``."""
    encoded = base64.b64encode(url.encode("utf-8")).decode("ascii")
    token = encoded.rstrip("=").replace("+", "-").replace("/", "_")
    return f"u!{token}"


def extract_html_file_attachments(html_content: str) -> list[InboundAttachment]:
    """Extract document attachments embedded as HTML links in Teams messages."""
    if not html_content or "<a" not in html_content:
        return []
    results: list[InboundAttachment] = []
    seen_urls: set[str] = set()
    for match in _FILE_LINK_RE.finditer(html_content):
        url = unescape(match.group("url"))
        raw_name = unescape(re.sub(r"<[^>]+>", "", match.group("name"))).strip()
        full_match = match.group(0)
        is_file = (
            "schema.skype.com/HyperLink/Files" in full_match
            or "sharepoint.com" in url.lower()
            or "1drv.ms" in url.lower()
            or any(raw_name.lower().endswith(ext) for ext in _DOC_EXTENSIONS)
        )
        if is_file and url not in seen_urls:
            seen_urls.add(url)
            results.append(
                InboundAttachment(
                    id=None,
                    name=raw_name or "document",
                    content_type="reference",
                    url=url,
                    raw={"href": url, "name": raw_name},
                )
            )
    return results


class TeamsGraphClient:
    """Microsoft Graph client for reading Teams message attachments and SharePoint files."""

    def __init__(
        self,
        app_id: str,
        app_password: str,
        tenant_id: str | None = None,
        *,
        http: httpx.AsyncClient | None = None,
        timeout: float = 15.0,
    ) -> None:
        self.app_id = (app_id or "").strip()
        self.app_password = (app_password or "").strip()
        self.tenant_id = (tenant_id or "").strip() or None
        self._http = http
        self._owns_http = http is None
        self.timeout = timeout
        self._token: str | None = None
        self._token_deadline: float = 0.0
        self._token_lock = asyncio.Lock()
        self._download_denied_until: float = 0.0

    async def aclose(self) -> None:
        if self._owns_http and self._http is not None:
            await self._http.aclose()
            self._http = None

    async def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=self.timeout)
        return self._http

    def effective_credentials(self) -> tuple[str, str, str]:
        """Credentials for Graph API: MS_GRAPH_* env overrides falling back to bot credentials."""
        app_id = (os.environ.get("MS_GRAPH_CLIENT_ID") or self.app_id).strip()
        secret = (os.environ.get("MS_GRAPH_CLIENT_SECRET") or self.app_password).strip()
        tenant = (
            os.environ.get("MS_GRAPH_TENANT_ID") or self.tenant_id or "botframework.com"
        ).strip()
        return app_id, secret, tenant

    async def get_token(self) -> str | None:
        """Fetch or return cached access token for Microsoft Graph."""
        now = time.monotonic()
        if self._token and now < self._token_deadline:
            return self._token
        async with self._token_lock:
            now = time.monotonic()
            if self._token and now < self._token_deadline:
                return self._token
            app_id, secret, tenant = self.effective_credentials()
            if not app_id or not secret:
                return None
            client = await self._client()
            url = f"{LOGIN_BASE}/{quote(tenant, safe='')}/oauth2/v2.0/token"
            try:
                resp = await client.post(
                    url,
                    data={
                        "grant_type": "client_credentials",
                        "client_id": app_id,
                        "client_secret": secret,
                        "scope": GRAPH_SCOPE,
                    },
                )
            except Exception as exc:
                log.warning("Graph token acquisition failed: %s", exc)
                return None
            if resp.status_code >= 400:
                log.warning("Graph token returned HTTP %s: %s", resp.status_code, resp.text[:200])
                return None
            payload = resp.json()
            token = payload.get("access_token")
            if not isinstance(token, str) or not token:
                return None
            expires_in = 3600
            try:
                expires_in = int(payload.get("expires_in") or 3600)
            except (ValueError, TypeError):
                expires_in = 3600
            self._token = token
            self._token_deadline = now + max(60, expires_in - 120)
            return token

    async def fetch_message_attachments(
        self, activity_data: dict[str, Any]
    ) -> list[InboundAttachment]:
        """Fetch reference attachments for an activity directly from Microsoft Graph API."""
        msg_id = activity_data.get("id")
        if not msg_id:
            return []
        token = await self.get_token()
        if not token:
            return []

        conv = activity_data.get("conversation") or {}
        conv_id = str(conv.get("id") or "")
        conv_type = str(conv.get("conversationType") or "")
        channel_data = activity_data.get("channelData") or {}
        team = channel_data.get("team") or {}
        team_id = str(team.get("id") or channel_data.get("teamsTeamId") or "")
        chan = channel_data.get("channel") or {}
        channel_id = str(channel_data.get("teamsChannelId") or chan.get("id") or "")

        in_channel = conv_type == "channel" or (
            "@thread.tacv2" in conv_id and bool(team_id and channel_id)
        )
        if in_channel:
            if not team_id or not channel_id:
                return []
            path = (
                f"/teams/{quote(team_id, safe='')}"
                f"/channels/{quote(channel_id, safe='')}"
                f"/messages/{quote(str(msg_id), safe='')}"
            )
        else:
            chat_id = conv_id.split(";", 1)[0]
            if not chat_id:
                return []
            path = f"/chats/{quote(chat_id, safe='')}/messages/{quote(str(msg_id), safe='')}"

        client = await self._client()
        try:
            resp = await client.get(
                f"{GRAPH_BASE}{path}",
                headers={"Authorization": f"Bearer {token}"},
            )
        except Exception as exc:
            log.warning("Graph fetch message failed for %s: %s", msg_id, exc)
            return []

        if resp.status_code >= 400:
            log.debug("Graph message get returned HTTP %s for %s", resp.status_code, msg_id)
            return []

        data = resp.json() if "json" in resp.headers.get("content-type", "") else {}
        attachments = data.get("attachments") or []
        results: list[InboundAttachment] = []
        for att in attachments:
            if not isinstance(att, dict):
                continue
            ctype = str(att.get("contentType") or "").lower()
            if any(ctype.startswith(prefix) for prefix in _CARD_TYPES):
                continue
            name = str(att.get("name") or "")
            url = str(att.get("contentUrl") or "")
            att_id = str(att.get("id") or "") or None
            if url or name:
                results.append(
                    InboundAttachment(
                        id=att_id,
                        name=name or "file",
                        content_type=att.get("contentType") or "reference",
                        url=url or None,
                        raw=att,
                    )
                )
        return results

    async def download_share(self, url: str) -> bytes | None:
        """Download file content from a SharePoint/OneDrive sharing URL or direct URL."""
        if not url:
            return None
        now = time.monotonic()
        if now < self._download_denied_until:
            return None

        token = await self.get_token()
        client = await self._client()

        # If it's a SharePoint/OneDrive sharing URL, use Graph /shares/ API
        if "sharepoint.com" in url.lower() or "1drv.ms" in url.lower():
            if not token:
                return None
            share_token = encode_sharing_url(url)
            share_url = f"{GRAPH_BASE}/shares/{share_token}/driveItem/content"
            try:
                resp = await client.get(
                    share_url,
                    headers={"Authorization": f"Bearer {token}"},
                    follow_redirects=True,
                )
            except Exception as exc:
                log.warning("Graph share download request failed: %s", exc)
                return None

            if resp.status_code in {401, 403}:
                self._download_denied_until = time.monotonic() + 300.0
                log.warning(
                    "Graph refused downloading file (HTTP %s). "
                    "Ensure the Azure AD App registration has 'Files.Read.All' permission.",
                    resp.status_code,
                )
                return None
            if resp.status_code >= 400:
                log.warning("Graph share download returned HTTP %s", resp.status_code)
                return None
            return resp.content

        # Fallback to direct GET with or without Bearer token
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            resp = await client.get(url, headers=headers, follow_redirects=True)
            if resp.is_success:
                return resp.content
            # Retry without auth header if external
            if headers:
                resp = await client.get(url, follow_redirects=True)
                if resp.is_success:
                    return resp.content
        except Exception as exc:
            log.warning("Direct download failed for %s: %s", url, exc)
        return None
