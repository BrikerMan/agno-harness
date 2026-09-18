"""Tests for unified environment configuration, logging service, and onboarding wizards."""

from __future__ import annotations

import json
import logging

from agno_harness import (
    LarkChannel,
    RelayConfig,
    RelayConsoleFormatter,
    RelayJsonFormatter,
    TeamsChannel,
    get_relay_logger,
    interactive_lark_onboarding,
    interactive_teams_onboarding,
    save_env_file,
    setup_relay_logging,
)


def test_relay_config_reads_only_harness_keys(monkeypatch):
    monkeypatch.setenv("AGNO_HARNESS_LLM_BASE_URL", "https://api.agno.ai/v1")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    assert RelayConfig.llm_base_url() == "https://api.agno.ai/v1"
    monkeypatch.delenv("AGNO_HARNESS_LLM_BASE_URL")
    assert RelayConfig.llm_base_url() == ""

    monkeypatch.setenv("AGNO_HARNESS_LLM_API_KEY", "sk-agno-123")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-456")
    assert RelayConfig.llm_api_key() == "sk-agno-123"
    monkeypatch.delenv("AGNO_HARNESS_LLM_API_KEY")
    assert RelayConfig.llm_api_key() == ""

    monkeypatch.setenv("AGNO_HARNESS_LLM_MODEL", "gpt-4o-mini")
    monkeypatch.setenv("OPENAI_MODEL_NAME", "gpt-4o")
    monkeypatch.setenv("MODEL", "ignored")
    assert RelayConfig.llm_model() == "gpt-4o-mini"


def test_channels_default_to_env_vars(monkeypatch):
    monkeypatch.setenv("AGNO_HARNESS_LARK_APP_ID", "cli_test_lark_111")
    monkeypatch.setenv("AGNO_HARNESS_LARK_APP_SECRET", "sec_test_lark_222")
    lark = LarkChannel()
    assert lark.app_id == "cli_test_lark_111"
    assert lark.app_secret == "sec_test_lark_222"

    # Explicit initialization overrides environment
    explicit_lark = LarkChannel(app_id="cli_explicit", app_secret="sec_explicit")
    assert explicit_lark.app_id == "cli_explicit"
    assert explicit_lark.app_secret == "sec_explicit"

    monkeypatch.setenv("AGNO_HARNESS_TEAMS_APP_ID", "teams-bot-id-333")
    monkeypatch.setenv("AGNO_HARNESS_TEAMS_APP_PASSWORD", "teams-bot-pwd-444")
    teams = TeamsChannel()
    assert teams.bot_app_id == "teams-bot-id-333"
    assert teams.bot_app_password == "teams-bot-pwd-444"


def test_save_env_file(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("EXISTING_KEY=old_val\n# A comment\nANOTHER_KEY=123\n")

    # Update existing and add new
    save_env_file(
        {"EXISTING_KEY": "new_val", "AGNO_HARNESS_LARK_APP_ID": "cli_999"},
        filepath=str(env_file),
    )

    content = env_file.read_text()
    assert "EXISTING_KEY=new_val\n" in content
    assert "# A comment\n" in content
    assert "ANOTHER_KEY=123\n" in content
    assert "AGNO_HARNESS_LARK_APP_ID=cli_999\n" in content


def test_interactive_onboarding_saves_keys(tmp_path):
    env_file = tmp_path / ".env"
    lark_keys = interactive_lark_onboarding(save_to_env=True, env_file=str(env_file))
    assert "AGNO_HARNESS_LARK_APP_ID" in lark_keys
    assert env_file.exists()
    assert "AGNO_HARNESS_LARK_APP_ID=" in env_file.read_text()

    teams_keys = interactive_teams_onboarding(save_to_env=True, env_file=str(env_file))
    assert "AGNO_HARNESS_TEAMS_APP_ID" in teams_keys
    assert "AGNO_HARNESS_TEAMS_APP_ID=" in env_file.read_text()


def test_relay_logging_console_and_json():
    handler = setup_relay_logging(level="DEBUG", fmt="console")
    assert handler is not None

    logger = get_relay_logger("lark.test")
    assert logger.name == "agno_harness.lark.test"
    record = logging.LogRecord(
        name="agno_harness.channels.lark",
        level=logging.INFO,
        pathname=__file__,
        lineno=10,
        msg="Inbound message received",
        args=(),
        exc_info=None,
    )
    record.chat_id = "chat-123"

    console_fmt = RelayConsoleFormatter()
    output = console_fmt.format(record)
    assert "[LARK]" in output
    assert "chat=chat-123" in output
    assert "Inbound message received" in output

    json_fmt = RelayJsonFormatter()
    json_output = json_fmt.format(record)
    parsed = json.loads(json_output)
    assert parsed["level"] == "INFO"
    assert parsed["logger"] == "agno_harness.channels.lark"
    assert parsed["chat_id"] == "chat-123"
    assert parsed["message"] == "Inbound message received"
