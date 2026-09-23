"""Teams onboard prints an install URL. It does not ask for a project directory."""

from __future__ import annotations

import pytest
from rich.console import Console

from agno_harness.helpers.teams import (
    TeamsOnboardError,
    assert_teams_portal_ready,
    format_link,
    format_teams_cli_failure,
    install_url_for,
    interactive_teams_onboarding,
    mask_secret,
    normalize_messaging_endpoint,
    parse_teams_cli_create,
    payload_from_teams_create_output,
    print_url,
    registration_from_app_get,
    split_link,
    teams_portal_token_message,
)


def _created_app(name: str, endpoint: str) -> dict[str, str | dict[str, str]]:
    return {
        "appName": name,
        "teamsAppId": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "botId": "11111111-2222-3333-4444-555555555555",
        "endpoint": endpoint,
        "installLink": "https://teams.microsoft.com/l/app/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "credentials": {
            "CLIENT_ID": "11111111-2222-3333-4444-555555555555",
            "CLIENT_SECRET": "secret-value",
            "TENANT_ID": "99999999-8888-7777-6666-555555555555",
        },
    }


def test_parse_teams_cli_create_maps_harness_keys_and_install_url():
    keys, install_url = parse_teams_cli_create(
        _created_app("DeskBot", "https://example.test/api/messages")
    )
    assert keys == {
        "AGNO_HARNESS_TEAMS_APP_ID": "11111111-2222-3333-4444-555555555555",
        "AGNO_HARNESS_TEAMS_APP_PASSWORD": "secret-value",
        "AGNO_HARNESS_TEAMS_TENANT_ID": "99999999-8888-7777-6666-555555555555",
    }
    assert install_url == "https://teams.microsoft.com/l/app/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def test_install_url_is_built_when_cli_omits_install_link():
    keys, install_url = parse_teams_cli_create(
        {
            "teamsAppId": "app-id",
            "credentials": {
                "CLIENT_ID": "client",
                "CLIENT_SECRET": "secret",
                "TENANT_ID": "tenant",
            },
        }
    )
    assert keys["AGNO_HARNESS_TEAMS_APP_ID"] == "client"
    assert install_url == install_url_for("app-id")


def test_parse_reads_credentials_from_dotenv_text():
    keys, install_url = parse_teams_cli_create(
        {"teamsAppId": "app-id", "installLink": "https://teams.microsoft.com/l/app/app-id"},
        dotenv={
            "CLIENT_ID": "from-env",
            "CLIENT_SECRET": "from-secret",
            "TENANT_ID": "from-tenant",
        },
    )
    assert keys["AGNO_HARNESS_TEAMS_APP_PASSWORD"] == "from-secret"
    assert install_url.endswith("/app-id")


def test_parse_rejects_payload_without_a_secret():
    with pytest.raises(TeamsOnboardError):
        parse_teams_cli_create({"teamsAppId": "app-id", "credentials": {"CLIENT_ID": "only-id"}})


def test_mask_secret_shows_ends_and_hides_the_middle():
    assert mask_secret("not-a-real-client-secret-xyz") == "not*******xyz"
    assert mask_secret("abc") == "a*******c"
    assert mask_secret("") == ""
    assert "secret-value" not in mask_secret("secret-value")


def test_split_link_breaks_on_query_and_joins_back():
    url = (
        "https://teams.microsoft.com/l/app/0d9a7803-55ce-4c13-a1f9-9fbc2a9d6fe5"
        "?installAppPackage=true&appTenantId=e295ebcb-6438-4e4c-ba2f-04e50addfbc0"
    )
    lines = split_link(url, 80)
    assert lines == [
        "https://teams.microsoft.com/l/app/0d9a7803-55ce-4c13-a1f9-9fbc2a9d6fe5",
        "?installAppPackage=true",
        "&appTenantId=e295ebcb-6438-4e4c-ba2f-04e50addfbc0",
    ]
    assert "".join(lines) == url
    assert all(len(line) <= 80 for line in lines)


def test_format_link_keeps_the_query_on_the_same_line():
    url = (
        "https://teams.microsoft.com/l/app/0d9a7803-55ce-4c13-a1f9-9fbc2a9d6fe5"
        "?installAppPackage=true&appTenantId=e295ebcb-6438-4e4c-ba2f-04e50addfbc0"
    )
    assert format_link(url + "\n") == url
    assert "\n" not in format_link(url)


def test_url_line_has_no_box_and_keeps_the_query(capsys):
    url = (
        "https://teams.microsoft.com/l/app/0d9a7803-55ce-4c13-a1f9-9fbc2a9d6fe5"
        "?installAppPackage=true&appTenantId=e295ebcb-6438-4e4c-ba2f-04e50addfbc0"
    )
    console = Console(width=40, force_terminal=False)
    print_url(
        console,
        "Install",
        url,
        summary="Open this link in Teams to add the bot to a chat.",
    )
    printed = capsys.readouterr().out
    assert url in printed
    assert "Open this link in Teams" in printed
    assert "╭" not in printed
    assert "╰" not in printed


def test_normalize_messaging_endpoint_uses_the_channel_path():
    path = "https://host.example/api/v1/channels/teams/messages"
    assert normalize_messaging_endpoint("https://host.example") == path
    assert normalize_messaging_endpoint("https://host.example/api/messages/") == path
    assert normalize_messaging_endpoint(path + "/") == path
    with pytest.raises(TeamsOnboardError):
        normalize_messaging_endpoint("http://host.example")


def test_onboard_prints_install_url_and_does_not_ask_for_a_project(tmp_path, capsys, monkeypatch):
    def explode(_prompt: str) -> str:
        raise AssertionError("onboard must not prompt")

    monkeypatch.setattr("builtins.input", explode)

    def create(name: str, endpoint: str) -> dict[str, str | dict[str, str]]:
        assert name == "DeskBot"
        assert endpoint == "https://example.test/api/v1/channels/teams/messages"
        return _created_app(name, endpoint)

    keys = interactive_teams_onboarding(
        prompt_input=True,
        name="DeskBot",
        endpoint="https://example.test/api/v1/channels/teams/messages",
        create_app=create,
    )
    output = capsys.readouterr().out
    assert keys["AGNO_HARNESS_TEAMS_APP_ID"] == "11111111-2222-3333-4444-555555555555"
    assert "https://teams.microsoft.com/l/app/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee" in output
    assert "AGNO_HARNESS_TEAMS_APP_PASSWORD=secret-value" in output
    assert "项目目录" not in output
    assert not (tmp_path / ".env").exists()
    assert not (tmp_path / "agent.py").exists()


def test_onboard_scaffolds_only_when_project_dir_is_passed(tmp_path):
    project = tmp_path / "teams-agent"
    interactive_teams_onboarding(
        save_to_env=False,
        name="DeskBot",
        endpoint="https://example.test/api/v1/channels/teams/messages",
        create_app=lambda name, endpoint: _created_app(name, endpoint),
        project_dir=str(project),
    )
    assert (project / "agent.py").is_file()
    assert (
        "AGNO_HARNESS_TEAMS_APP_ID=11111111-2222-3333-4444-555555555555"
        in (project / ".env").read_text()
    )


def test_onboard_without_cli_prints_portal_url(capsys):
    keys = interactive_teams_onboarding(prompt_input=False, save_to_env=False)
    output = capsys.readouterr().out
    assert keys == {}
    assert "https://dev.teams.microsoft.com/apps" in output
    assert "teams app create" in output
    assert "QR" in output


def test_payload_from_cli_text_keeps_install_url_and_env_credentials():
    text = """
App created successfully!
Teams App ID: aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee
  ▸ Install in Teams    → https://teams.microsoft.com/l/app/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee?installAppPackage=true
"""
    payload = payload_from_teams_create_output(
        text,
        {
            "CLIENT_ID": "11111111-2222-3333-4444-555555555555",
            "CLIENT_SECRET": "secret-value",
            "TENANT_ID": "99999999-8888-7777-6666-555555555555",
        },
    )
    keys, install_url = parse_teams_cli_create(payload)
    assert keys["AGNO_HARNESS_TEAMS_APP_PASSWORD"] == "secret-value"
    assert install_url.startswith("https://teams.microsoft.com/l/app/aaaaaaaa")


def test_registration_from_app_get_reads_install_link_and_callback():
    missing = registration_from_app_get(
        {
            "installLink": "https://teams.microsoft.com/l/app/app-id?installAppPackage=true",
            "portalLink": "https://dev.teams.microsoft.com/apps/app-id",
            "endpoint": None,
        }
    )
    assert missing.install_url.startswith("https://teams.microsoft.com/l/app/")
    assert missing.callback_configured is False

    configured = registration_from_app_get({"endpoint": "https://bot.example/api/messages"})
    assert configured.callback_configured is True
    assert configured.callback_url == "https://bot.example/api/messages"


def test_doctor_prints_install_link_and_unconfigured_callback(tmp_path, monkeypatch):
    import importlib

    from typer.testing import CliRunner

    from agno_harness.helpers.teams import TeamsAppRegistration

    cli_module = importlib.import_module("agno_harness.cli.main")
    env_file = tmp_path / ".env"
    env_file.write_text(
        "AGNO_HARNESS_TEAMS_APP_ID=aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee\n"
        "AGNO_HARNESS_TEAMS_APP_PASSWORD=secret\n"
        "AGNO_HARNESS_TEAMS_TENANT_ID=99999999-8888-7777-6666-555555555555\n",
        encoding="utf-8",
    )

    def fake_registration(app_id: str) -> TeamsAppRegistration:
        assert app_id == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        return TeamsAppRegistration(
            install_url="https://teams.microsoft.com/l/app/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            portal_url="https://dev.teams.microsoft.com/apps/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        )

    monkeypatch.setenv("AGNO_HARNESS_TEAMS_APP_ID", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    monkeypatch.setenv("AGNO_HARNESS_TEAMS_APP_PASSWORD", "secret")
    monkeypatch.setenv("AGNO_HARNESS_TEAMS_TENANT_ID", "99999999-8888-7777-6666-555555555555")
    monkeypatch.setattr(cli_module, "fetch_teams_app_registration", fake_registration)
    result = CliRunner().invoke(cli_module.app, ["teams", "doctor", "--env-file", str(env_file)])
    assert "https://teams.microsoft.com/l/app/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee" in result.output
    assert "Not configured" in result.output
    assert "teams app update" in result.output
    assert "ngrok http 8000" in result.output
    assert result.exit_code == 1


def test_missing_portal_token_is_explained_without_asking_to_login_again():
    message = teams_portal_token_message("eliyar@yodo1.com")
    assert "eliyar@yodo1.com" in message
    assert "teams logout" in message
    assert "teams login" in message
    raw = """
    {
      "ok": false,
      "error": {
        "code": "AUTH_TOKEN_FAILED",
        "message": "Failed to get Graph token.",
        "suggestion": "Try `teams login` again."
      }
    }
    """
    assert format_teams_cli_failure(raw) == teams_portal_token_message()


def test_onboard_stops_before_prompts_when_portal_token_is_missing(monkeypatch):
    def explode(_prompt: str) -> str:
        raise AssertionError("onboard must not prompt")

    monkeypatch.setattr("builtins.input", explode)

    with pytest.raises(TeamsOnboardError, match="teams logout"):
        interactive_teams_onboarding(
            prompt_input=True,
            use_cli=True,
            read_status=lambda: {
                "loggedIn": True,
                "username": "eliyar@yodo1.com",
                "tdp": {"connected": False},
            },
        )
    with pytest.raises(TeamsOnboardError, match="Not logged in"):
        assert_teams_portal_ready({"loggedIn": False, "tdp": {"connected": False}})
