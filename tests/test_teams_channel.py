"""Contract tests for the built-in Teams connector, JWT check, and webhook."""

from __future__ import annotations

import ast
import base64
import json
import re
import time
from typing import Any

import httpx
import pytest
from Crypto.Hash import SHA256
from Crypto.PublicKey import RSA
from Crypto.Signature import pkcs1_15
from fastapi import FastAPI
from typer.testing import CliRunner

from agno_harness import (
    AgentRuntime,
    OutboundMessage,
    RelayApp,
    TeamsChannel,
    TeamsCredentialsError,
    TeamsDeliveryError,
    TeamsServiceUrlError,
    teams_messaging_endpoint,
)
from agno_harness.channels.teams import teams_topic_id
from agno_harness.channels.teams_auth import TeamsJwtVerifier
from agno_harness.channels.teams_connector import (
    TeamsConnectorClient,
    TeamsConversationRef,
    teams_token_authority,
    validate_service_url,
)
from agno_harness.cli.main import app as cli_app
from agno_harness.helpers.teams import inspect_teams_config

from .conftest import FakeAgent, content, run_completed

_PRIVATE = RSA.generate(2048)
_APP_ID = "app-id"
_PASSWORD = "secret"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


@pytest.fixture(autouse=True)
def _clear_teams_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "AGNO_HARNESS_TEAMS_APP_ID",
        "AGNO_HARNESS_TEAMS_APP_PASSWORD",
        "AGNO_HARNESS_TEAMS_TENANT_ID",
    ):
        monkeypatch.delenv(name, raising=False)


def _token(*, audience: str = _APP_ID, issuer: str = "https://api.botframework.com") -> str:
    now = int(time.time())
    header = _b64(json.dumps({"alg": "RS256", "kid": "test-key"}).encode())
    payload = _b64(json.dumps({"aud": audience, "iss": issuer, "exp": now + 600}).encode())
    signing_input = f"{header}.{payload}".encode()
    signature = pkcs1_15.new(_PRIVATE).sign(SHA256.new(signing_input))
    return f"{header}.{payload}.{_b64(signature)}"


def _verifier() -> TeamsJwtVerifier:
    return TeamsJwtVerifier(_APP_ID, keys={"test-key": _PRIVATE.public_key()})


def _activity(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "message",
        "id": "act-1",
        "timestamp": "2026-09-22T06:00:00.000Z",
        "serviceUrl": "https://smba.trafficmanager.net/amer/",
        "channelId": "msteams",
        "text": "hello",
        "from": {"id": "user-1", "name": "Ada", "aadObjectId": "aad-1"},
        "recipient": {"id": "bot-1", "name": "Bot"},
        "conversation": {"id": "19:chat@thread.v2", "conversationType": "personal"},
        "channelData": {"tenant": {"id": "tenant-1"}},
    }
    payload.update(overrides)
    return payload


def _channel(**kwargs: Any) -> TeamsChannel:
    http = kwargs.pop("http", None)
    connector = kwargs.pop("connector", None)
    if connector is None and http is not None:
        connector = TeamsConnectorClient(
            kwargs.get("bot_app_id", _APP_ID),
            kwargs.get("bot_app_password", _PASSWORD),
            kwargs.get("tenant_id"),
            http=http,
        )
    return TeamsChannel(
        bot_app_id=kwargs.pop("bot_app_id", _APP_ID),
        bot_app_password=kwargs.pop("bot_app_password", _PASSWORD),
        verifier=kwargs.pop("verifier", _verifier()),
        connector=connector,
        **kwargs,
    )


def _scripted_http(calls: list[httpx.Request]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.endswith("/oauth2/v2.0/token"):
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        body = json.loads(request.content)
        assert request.headers["authorization"] == "Bearer tok"
        return httpx.Response(201, json={"id": f"out-{body['type']}"})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _post(
    channel: TeamsChannel, payload: dict[str, Any], *, token: str | None
) -> httpx.Response:
    app = FastAPI()
    app.include_router(channel.get_router())
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post("/api/messages", json=payload, headers=headers)


@pytest.mark.parametrize(
    "service_url",
    [
        "http://smba.trafficmanager.net/amer/",
        "https://evil.example/steal",
        "https://botframework.com.evil.example/",
        "https://smba.trafficmanager.net.evil.example/",
        "https://169.254.169.254/",
        "https://user:pass@smba.trafficmanager.net/amer",
        "https://smba.trafficmanager.net:8443/amer",
    ],
)
def test_service_url_allowlist_rejects(service_url: str) -> None:
    with pytest.raises(TeamsServiceUrlError):
        validate_service_url(service_url)


@pytest.mark.parametrize(
    ("service_url", "expected"),
    [
        ("https://smba.trafficmanager.net/amer/", "https://smba.trafficmanager.net/amer"),
        ("https://token.botframework.com", "https://token.botframework.com"),
        ("https://botframework.azure.us/x", "https://botframework.azure.us/x"),
    ],
)
def test_service_url_allowlist_accepts(service_url: str, expected: str) -> None:
    assert validate_service_url(service_url) == expected


def test_token_authority_single_and_multi_tenant() -> None:
    assert teams_token_authority(None) == "https://login.microsoftonline.com/botframework.com"
    assert teams_token_authority("  ") == "https://login.microsoftonline.com/botframework.com"
    assert teams_token_authority("contoso") == "https://login.microsoftonline.com/contoso"
    with pytest.raises(TeamsCredentialsError):
        teams_token_authority("contoso/admin")


def test_topic_id_follows_channel_thread_not_team() -> None:
    threaded = _activity(
        conversation={
            "id": "19:channel@thread.tacv2;messageid=root-99",
            "conversationType": "channel",
        },
        channelData={"teamsChannelId": "19:channel", "teamsTeamId": "team-1"},
    )
    assert teams_topic_id(threaded) == "root-99"

    root = _activity(
        conversation={"id": "19:channel@thread.tacv2", "conversationType": "channel"},
        channelData={"teamsChannelId": "19:channel", "teamsTeamId": "team-1"},
    )
    assert teams_topic_id(root) == "19:channel"
    assert teams_topic_id(_activity()) is None


def test_bot_tenant_id_is_an_alias() -> None:
    channel = _channel(bot_tenant_id="tenant-9")
    assert channel.tenant_id == "tenant-9"
    assert channel.bot.connector.authority.endswith("/tenant-9")
    with pytest.raises(ValueError):
        _channel(tenant_id="one", bot_tenant_id="two")


def test_messaging_endpoint_is_api_messages() -> None:
    assert teams_messaging_endpoint("https://bot.example") == "https://bot.example/api/messages"
    assert (
        teams_messaging_endpoint("https://bot.example/", prefix="agent")
        == "https://bot.example/agent/api/messages"
    )


@pytest.mark.asyncio
async def test_webhook_rejects_missing_and_invalid_tokens() -> None:
    channel = _channel()
    missing = await _post(channel, _activity(), token=None)
    assert missing.status_code == 401

    rejected = await _post(channel, _activity(), token=_token(audience="someone-else"))
    assert rejected.status_code == 401
    assert channel._inbound_queue.empty()


@pytest.mark.asyncio
async def test_webhook_without_credentials_is_503_not_200() -> None:
    channel = TeamsChannel(bot_app_id=_APP_ID, verifier=_verifier())
    response = await _post(channel, _activity(), token=_token())
    assert response.status_code == 503


@pytest.mark.asyncio
async def test_webhook_accepts_message_and_caches_conversation() -> None:
    channel = _channel()
    response = await _post(channel, _activity(text="<at>Bot</at> hi"), token=_token())
    assert response.status_code == 200
    event = channel._inbound_queue.get_nowait()
    assert event.key.chat_id == "19:chat@thread.v2"
    assert event.key.is_direct_message is True
    assert event.key.sender_id == "aad-1"
    ref = channel.bot.ref_for(event.key.chat_id)
    assert ref.service_url == "https://smba.trafficmanager.net/amer"
    assert ref.activity_id == "act-1"


@pytest.mark.asyncio
async def test_webhook_invoke_becomes_card_action() -> None:
    channel = _channel()
    payload = _activity(
        type="invoke",
        name="adaptiveCard/action",
        text="",
        conversation={"id": "19:group", "conversationType": "groupChat"},
        value={"action": {"data": {"action_id": "agno.hitl.resume", "accepted": True}}},
    )
    response = await _post(channel, payload, token=_token())
    assert response.status_code == 200
    body = response.json()
    assert body["type"] == "application/vnd.microsoft.activity.invokeResponse"
    event = channel._inbound_queue.get_nowait()
    assert event.action_id == "agno.hitl.resume"
    assert event.action_value["accepted"] is True


@pytest.mark.asyncio
async def test_webhook_ignores_conversation_update_and_rejects_bad_service_url() -> None:
    channel = _channel()
    ignored = await _post(channel, _activity(type="conversationUpdate"), token=_token())
    assert ignored.status_code == 200
    assert channel._inbound_queue.empty()

    rejected = await _post(
        channel,
        _activity(serviceUrl="https://evil.example/collect"),
        token=_token(),
    )
    assert rejected.status_code == 400
    assert channel._inbound_queue.empty()


@pytest.mark.asyncio
async def test_send_and_typing_hit_the_connector() -> None:
    calls: list[httpx.Request] = []
    channel = _channel(http=_scripted_http(calls))
    channel.bot.remember(_activity())
    destination = channel._event_from_activity(_activity()).key

    typing_id = None
    await channel.typing(destination, True)
    sent = await channel.send(destination, OutboundMessage(text="hello"))
    await channel.typing(destination, False)

    assert sent == "out-message"
    assert not sent.startswith("teams_msg_")
    assert typing_id is None
    bodies = [json.loads(call.content) for call in calls if not call.url.path.endswith("/token")]
    assert [body["type"] for body in bodies] == ["typing", "message"]
    assert bodies[1]["text"] == "hello"
    assert "smba.trafficmanager.net" in str(calls[-1].url)
    assert "/v3/conversations/" in str(calls[-1].url)


@pytest.mark.asyncio
async def test_send_without_credentials_or_conversation_fails_loud() -> None:
    bare = TeamsChannel(bot_app_id=_APP_ID)
    key = channel_key()
    with pytest.raises(TeamsCredentialsError):
        await bare.start()
    with pytest.raises(TeamsCredentialsError):
        await bare.send(key, OutboundMessage(text="hi"))

    channel = _channel()
    with pytest.raises(TeamsDeliveryError):
        await channel.send(key, OutboundMessage(text="hi"))


@pytest.mark.asyncio
async def test_send_refuses_non_botframework_service_url() -> None:
    calls: list[httpx.Request] = []
    channel = _channel(http=_scripted_http(calls))
    channel.bot._conversations["chat"] = TeamsConversationRef(
        service_url="https://evil.example/collect",
        conversation_id="chat",
        activity_id="act-1",
        bot_id="bot-1",
        bot_name="Bot",
        user_id="user-1",
        user_name="Ada",
    )
    with pytest.raises(TeamsServiceUrlError):
        await channel.send(channel_key("chat"), OutboundMessage(text="hi"))
    assert calls == []


@pytest.mark.asyncio
async def test_custom_adapter_must_return_a_real_id() -> None:
    class EmptyAdapter:
        async def send_reply(self, destination: Any, message: Any, attachments: list[Any]) -> str:
            return ""

    channel = _channel(adapter=EmptyAdapter(), bot_app_password="")
    with pytest.raises(TeamsDeliveryError):
        await channel.send(channel_key(), OutboundMessage(text="hi"))


@pytest.mark.asyncio
async def test_group_messages_are_mention_only_and_invokes_still_run() -> None:
    calls: list[httpx.Request] = []
    channel = _channel(http=_scripted_http(calls))
    agent = FakeAgent([content("Answer"), run_completed()])
    runtime = AgentRuntime(agent=agent)
    relay = RelayApp(runtime)
    relay.add_channel(channel)

    chatter = channel._event_from_activity(
        _activity(
            id="chatter",
            text="hello everyone",
            conversation={"id": "19:group", "conversationType": "groupChat"},
        )
    )
    assert await relay.handle_event(channel, chatter) is None
    assert agent.last_kwargs == {}

    mentioned = _activity(
        id="mentioned",
        text="<at>Bot</at> hi",
        conversation={"id": "19:group", "conversationType": "groupChat"},
        entities=[{"type": "mention", "mentioned": {"id": "bot-1", "name": "Bot"}}],
    )
    channel.bot.remember(mentioned)
    outbound = await relay.handle_event(channel, channel._event_from_activity(mentioned))
    assert outbound is not None
    assert "Answer" in outbound.text
    assert agent.last_kwargs != {}

    invoke = channel._event_from_activity(
        _activity(
            id="invoke-1",
            type="invoke",
            text="",
            conversation={"id": "19:group", "conversationType": "groupChat"},
            value={"action_id": "unknown.action"},
        )
    )
    agent.last_kwargs = {}
    handled = await relay.handle_event(channel, invoke)
    assert handled is not None
    assert agent.last_kwargs == {}


def test_doctor_reports_endpoint_and_missing_credentials(tmp_path: Any) -> None:
    report = inspect_teams_config(public_base="https://bot.example", prefix="/agent")
    assert report.ok is False
    assert report.messaging_endpoint == "https://bot.example/agent/api/messages"
    assert any("APP_ID" in problem for problem in report.problems)

    env_file = tmp_path / ".env"
    env_file.write_text("\n", encoding="utf-8")
    runner = CliRunner()
    result = runner.invoke(cli_app, ["teams", "doctor", "--env-file", str(env_file)])
    assert result.exit_code == 1
    assert "AGNO_HARNESS_TEAMS_APP_ID" in result.output


def _parse_project(root: Any) -> None:
    for path in root.rglob("*.py"):
        ast.parse(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("channel", "calls"),
    [
        ("all", ("mount_web", "mount_teams", "mount_lark", "mount_cli")),
        ("cli", ()),
        ("web", ("mount_web",)),
        ("teams", ("mount_web", "mount_teams")),
        ("lark", ("mount_lark",)),
    ],
)
def test_init_mounts_channels_with_explicit_calls(
    tmp_path: Any, channel: str, calls: tuple[str, ...]
) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli_app,
        ["init", str(tmp_path), "--channel", channel, "--with-knowledge"],
    )
    assert result.exit_code == 0
    _parse_project(tmp_path)
    mounted = (tmp_path / "app" / "channels" / "__init__.py").read_text(encoding="utf-8")
    assert "ENABLED" not in mounted
    for name in ("mount_web", "mount_teams", "mount_lark", "mount_cli"):
        called = re.search(rf"{name}\(\s*relay\s*,\s*app\b", mounted)
        if name in calls:
            assert called
        else:
            assert called is None
    teams_mount = (tmp_path / "app" / "channels" / "teams.py").read_text(encoding="utf-8")
    mount_body = teams_mount.split("def mount", 1)[1]
    assert "log.error" in mount_body
    assert "raise" in mount_body
    assert "return" not in mount_body
    assert (tmp_path / "app" / "knowledge" / "memory.md").is_file()
    assert (tmp_path / "app" / "agents" / "agent_builder.py").is_file()
    assert "data/" in (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert "MUST CHANGE BEFORE PRODUCTION" in (tmp_path / "app" / "identity.py").read_text(
        encoding="utf-8"
    )
    if channel in {"all", "teams"}:
        assert "build_teams_resolver" in mounted
    else:
        assert "build_teams_resolver" not in mounted
    assert "EMBEDDING" not in (tmp_path / ".env.example").read_text(encoding="utf-8")
    entry = (tmp_path / "agent.py").read_text(encoding="utf-8")
    if channel == "cli":
        assert "from app.channels.cli import mount" in entry
        assert "mount_all(" not in entry
        assert "uvicorn" not in entry
    else:
        assert "create_app" in entry


def test_init_cli_still_writes_notes_and_keeps_create_app(tmp_path: Any) -> None:
    runner = CliRunner()
    result = runner.invoke(cli_app, ["init", str(tmp_path), "--channel", "cli"])
    assert result.exit_code == 0
    assert (tmp_path / "app" / "knowledge" / "memory.md").is_file()
    assert "def create_app" in (tmp_path / "app" / "main.py").read_text(encoding="utf-8")


def test_init_rejects_unknown_channel(tmp_path: Any) -> None:
    runner = CliRunner()
    result = runner.invoke(cli_app, ["init", str(tmp_path), "--channel", "slack"])
    assert result.exit_code == 1
    assert not (tmp_path / "agent.py").exists()


def channel_key(chat_id: str = "19:chat@thread.v2") -> Any:
    from agno_harness import ConversationKey

    return ConversationKey(platform="teams", chat_id=chat_id, is_direct_message=True)
