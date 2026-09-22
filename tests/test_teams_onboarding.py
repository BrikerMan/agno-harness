"""Teams onboard prints an install URL. It does not ask for a project directory."""

from __future__ import annotations

import pytest

from agno_harness.helpers.teams import (
    TeamsOnboardError,
    assert_teams_portal_ready,
    format_teams_cli_failure,
    install_url_for,
    interactive_teams_onboarding,
    normalize_messaging_endpoint,
    parse_teams_cli_create,
    payload_from_teams_create_output,
    registration_from_app_get,
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


def test_normalize_messaging_endpoint_appends_api_messages():
    assert (
        normalize_messaging_endpoint("https://host.example") == "https://host.example/api/messages"
    )
    assert (
        normalize_messaging_endpoint("https://host.example/api/messages/")
        == "https://host.example/api/messages"
    )
    with pytest.raises(TeamsOnboardError):
        normalize_messaging_endpoint("http://host.example")


def test_onboard_prints_install_url_and_does_not_ask_for_a_project(tmp_path, capsys, monkeypatch):
    def explode(_prompt: str) -> str:
        raise AssertionError("onboard must not prompt")

    monkeypatch.setattr("builtins.input", explode)

    def create(name: str, endpoint: str) -> dict[str, str | dict[str, str]]:
        assert name == "DeskBot"
        assert endpoint == "https://example.test/api/messages"
        return _created_app(name, endpoint)

    keys = interactive_teams_onboarding(
        prompt_input=True,
        name="DeskBot",
        endpoint="https://example.test/api/messages",
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
        endpoint="https://example.test/api/messages",
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

    async def fake_token(_app_id: str, _password: str, _tenant: str | None) -> tuple[bool, str]:
        return True, "token ok via test"

    monkeypatch.setenv("AGNO_HARNESS_TEAMS_APP_ID", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    monkeypatch.setenv("AGNO_HARNESS_TEAMS_APP_PASSWORD", "secret")
    monkeypatch.setenv("AGNO_HARNESS_TEAMS_TENANT_ID", "99999999-8888-7777-6666-555555555555")
    monkeypatch.setattr(cli_module, "fetch_teams_app_registration", fake_registration)
    monkeypatch.setattr(cli_module, "probe_teams_token", fake_token)
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
