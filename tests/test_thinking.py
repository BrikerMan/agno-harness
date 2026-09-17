"""Thinking level → Agno ``extra_body`` mapping used by the demo."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

DEMO_BACKEND = Path(__file__).resolve().parents[1] / "examples" / "demo" / "backend"
if str(DEMO_BACKEND) not in sys.path:
    sys.path.insert(0, str(DEMO_BACKEND))

thinking = pytest.importorskip("app.thinking")


class TestThinkingMapping:
    def test_none_disables_thinking(self):
        assert thinking.extra_body_for("none") == {
            "chat_template_kwargs": {"enable_thinking": False}
        }

    def test_medium_sets_budget(self):
        body = thinking.extra_body_for("medium")
        assert body["thinking_token_budget"] == 1500
        assert body["thinking_budget"] == 1500

    def test_auto_defaults_to_1500(self):
        assert thinking.normalize_level("auto") == "auto"
        assert thinking.normalize_level("default") == "auto"
        body = thinking.extra_body_for("auto")
        assert body["thinking_token_budget"] == 1500
        assert body["thinking_budget"] == 1500

    def test_apply_stamps_extra_body_and_max_tokens(self):
        model = SimpleNamespace(extra_body=None, max_tokens=None)
        thinking.apply_thinking([model], "high")
        assert model.extra_body["thinking_token_budget"] == 5000
        assert model.max_tokens == 12288

        thinking.apply_thinking([model], "none")
        assert model.extra_body["chat_template_kwargs"]["enable_thinking"] is False
        assert model.max_tokens == 2048

    def test_models_of_walks_subagent_toolkit(self):
        from agno_relay.tools.subagent import SubAgentToolkit

        parent_model = SimpleNamespace(extra_body=None, max_tokens=None)
        child_model = SimpleNamespace(extra_body=None, max_tokens=None)
        child = SimpleNamespace(name="reviewer", model=child_model, description="x")
        toolkit = SubAgentToolkit(agents=[child])
        parent = SimpleNamespace(model=parent_model, tools=[toolkit])

        models = thinking.models_of([parent])
        assert parent_model in models
        assert child_model in models

    async def test_hook_reads_forwarded_props(self):
        model = SimpleNamespace(extra_body=None, max_tokens=None)
        agent = SimpleNamespace(model=model, tools=[])
        hook = thinking.make_thinking_hook([agent])
        data: dict = {}
        scope = SimpleNamespace(
            forwarded_props={"reasoning": "extra-high"},
            module_data=lambda name: data.setdefault(name, {}),
        )
        async for _ in hook(scope):
            pass
        assert model.extra_body["thinking_token_budget"] == 10000
        assert data["thinking"]["level"] == "extra-high"
        assert thinking.current_level() == "extra-high"

    def test_apply_current_thinking_uses_context(self):
        model = SimpleNamespace(extra_body=None, max_tokens=None)
        thinking._current_level.set("low")
        thinking.apply_current_thinking(model)
        assert model.extra_body["thinking_token_budget"] == 500
