"""Tests for Lark automated Scan-to-Create QR onboarding and helper functions."""

from unittest.mock import MagicMock, patch

import httpx
import pytest

from agno_harness.helpers.lark import (
    FEISHU_ACCOUNTS_URL,
    LARK_INTERNATIONAL_ACCOUNTS_URL,
    begin_lark_app_registration,
    fetch_lark_app_info,
    interactive_lark_onboarding,
    poll_lark_app_registration,
)


def test_begin_lark_app_registration_success():
    def mock_post(url, data, headers):
        assert url == f"{FEISHU_ACCOUNTS_URL}/oauth/v1/app/registration"
        assert data["action"] == "begin"
        assert data["archetype"] == "PersonalAgent"
        return httpx.Response(
            200,
            json={
                "device_code": "dev-code-123",
                "user_code": "ABCD-1234",
                "verification_uri": "https://open.feishu.cn/page/launcher",
                "verification_uri_complete": "https://open.feishu.cn/page/launcher?user_code=ABCD-1234",
                "expires_in": 1800,
                "interval": 3,
            },
            request=httpx.Request("POST", url),
        )

    mock_client = MagicMock(spec=httpx.Client)
    mock_client.post.side_effect = mock_post

    res = begin_lark_app_registration(domain="feishu", client=mock_client)
    assert res["device_code"] == "dev-code-123"
    assert res["user_code"] == "ABCD-1234"
    assert "https://open.feishu.cn/page/launcher?user_code=ABCD-1234" in res["verification_url"]
    assert "from=agno_harness" in res["qr_url"]
    assert res["expires_in"] == 1800
    assert res["interval"] == 3


def test_begin_lark_app_registration_lark_domain():
    def mock_post(url, data, headers):
        assert url == f"{LARK_INTERNATIONAL_ACCOUNTS_URL}/oauth/v1/app/registration"
        return httpx.Response(
            200,
            json={
                "device_code": "dev-code-lark",
                "user_code": "LARK-9999",
                "verification_uri_complete": "https://open.larksuite.com/page/launcher?user_code=LARK-9999",
            },
            request=httpx.Request("POST", url),
        )

    mock_client = MagicMock(spec=httpx.Client)
    mock_client.post.side_effect = mock_post

    res = begin_lark_app_registration(domain="lark", client=mock_client)
    assert res["device_code"] == "dev-code-lark"
    assert "open.larksuite.com" in res["verification_url"]


def test_begin_lark_app_registration_http_error():
    mock_client = MagicMock(spec=httpx.Client)
    mock_client.post.return_value = httpx.Response(
        500, text="Internal Server Error", request=httpx.Request("POST", "http://test")
    )
    with pytest.raises(RuntimeError, match="Lark app registration failed"):
        begin_lark_app_registration(client=mock_client)


def test_begin_lark_app_registration_api_error():
    mock_client = MagicMock(spec=httpx.Client)
    mock_client.post.return_value = httpx.Response(
        200,
        json={"error": "invalid_request", "error_description": "Bad Request"},
        request=httpx.Request("POST", "http://test"),
    )
    with pytest.raises(RuntimeError, match="Bad Request"):
        begin_lark_app_registration(client=mock_client)


def test_poll_lark_app_registration_success():
    calls = []

    def mock_post(url, data, headers):
        calls.append(data)
        if len(calls) == 1:
            return httpx.Response(
                200,
                json={"error": "authorization_pending"},
                request=httpx.Request("POST", url),
            )
        elif len(calls) == 2:
            return httpx.Response(
                200,
                json={
                    "client_id": "cli_test_123456",
                    "client_secret": "sec_test_secret_789",
                    "user_info": {
                        "open_id": "ou_creator_open_id",
                        "tenant_brand": "feishu",
                    },
                },
                request=httpx.Request("POST", url),
            )

    mock_client = MagicMock(spec=httpx.Client)
    mock_client.post.side_effect = mock_post

    ticks = []

    def on_tick(cnt):
        ticks.append(cnt)

    res = poll_lark_app_registration(
        device_code="dev-code-xyz",
        interval=1,
        expires_in=60,
        client=mock_client,
        on_tick=on_tick,
        sleep_func=lambda s: None,
    )

    assert res["app_id"] == "cli_test_123456"
    assert res["app_secret"] == "sec_test_secret_789"
    assert res["user_open_id"] == "ou_creator_open_id"
    assert len(ticks) == 2


def test_poll_lark_app_registration_access_denied():
    mock_client = MagicMock(spec=httpx.Client)
    mock_client.post.return_value = httpx.Response(
        200,
        json={"error": "access_denied"},
        request=httpx.Request("POST", "http://test"),
    )
    with pytest.raises(PermissionError, match="已被用户取消/拒绝"):
        poll_lark_app_registration(
            device_code="dev-code-xyz",
            client=mock_client,
            sleep_func=lambda s: None,
        )


def test_poll_lark_app_registration_expired():
    mock_client = MagicMock(spec=httpx.Client)
    mock_client.post.return_value = httpx.Response(
        200,
        json={"error": "expired_token"},
        request=httpx.Request("POST", "http://test"),
    )
    with pytest.raises(TimeoutError, match="二维码已过期"):
        poll_lark_app_registration(
            device_code="dev-code-xyz",
            client=mock_client,
            sleep_func=lambda s: None,
        )


def test_fetch_lark_app_info():
    with patch("agno_harness.helpers.lark.httpx.Client") as MockClient:
        instance = MockClient.return_value.__enter__.return_value

        def mock_post(url, json):
            return httpx.Response(
                200,
                json={"code": 0, "tenant_access_token": "t-token-123"},
                request=httpx.Request("POST", url),
            )

        def mock_get(url, headers):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "app": {
                            "app_name": "My Agent Bot",
                            "description": "Assistant description",
                            "avatar": {"avatar_72": "https://avatar.png"},
                        }
                    },
                },
                request=httpx.Request("GET", url),
            )

        instance.post.side_effect = mock_post
        instance.get.side_effect = mock_get

        info = fetch_lark_app_info("cli_test", "sec_test")
        assert info is not None
        assert info["app_name"] == "My Agent Bot"
        assert info["description"] == "Assistant description"


def test_interactive_lark_onboarding_scan_flow(tmp_path):
    env_file = tmp_path / ".env"

    with (
        patch("agno_harness.helpers.lark.begin_lark_app_registration") as mock_begin,
        patch("agno_harness.helpers.lark.poll_lark_app_registration") as mock_poll,
        patch("agno_harness.helpers.lark.print_terminal_qr") as mock_qr,
        patch("agno_harness.helpers.lark.fetch_lark_app_info") as mock_info,
    ):
        mock_begin.return_value = {
            "device_code": "dc-123",
            "verification_url": "https://open.feishu.cn/page/launcher?user_code=TEST",
            "qr_url": "https://open.feishu.cn/page/launcher?user_code=TEST&from=agno_harness",
            "interval": 2,
            "expires_in": 300,
        }
        mock_poll.return_value = {
            "app_id": "cli_auto_generated_999",
            "app_secret": "sec_auto_secret_888",
            "user_open_id": "ou_user_111",
            "brand": "feishu",
        }
        mock_info.return_value = {"app_name": "ScanBot"}

        keys = interactive_lark_onboarding(
            prompt_input=True,
            save_to_env=True,
            env_file=str(env_file),
            manual=False,
        )

        assert keys["AGNO_HARNESS_LARK_APP_ID"] == "cli_auto_generated_999"
        assert keys["AGNO_HARNESS_LARK_APP_SECRET"] == "sec_auto_secret_888"
        assert keys["AGNO_HARNESS_LARK_USER_OPEN_ID"] == "ou_user_111"

        mock_qr.assert_called_once()
        content = env_file.read_text()
        assert "AGNO_HARNESS_LARK_APP_ID=cli_auto_generated_999" in content
        assert "AGNO_HARNESS_LARK_USER_OPEN_ID=ou_user_111" in content
        written = {line.split("=", 1)[0] for line in content.splitlines() if "=" in line}
        assert "LARK_APP_ID" not in written


def test_interactive_lark_onboarding_manual_flow(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    inputs = iter(["cli_manual_123", "sec_manual_456"])
    monkeypatch.setattr("builtins.input", lambda *args: next(inputs))

    keys = interactive_lark_onboarding(
        prompt_input=True,
        save_to_env=True,
        env_file=str(env_file),
        manual=True,
    )

    assert keys["AGNO_HARNESS_LARK_APP_ID"] == "cli_manual_123"
    assert keys["AGNO_HARNESS_LARK_APP_SECRET"] == "sec_manual_456"
    content = env_file.read_text()
    assert "AGNO_HARNESS_LARK_APP_ID=cli_manual_123" in content
