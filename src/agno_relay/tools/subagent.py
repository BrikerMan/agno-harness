"""Generic sub-agent delegation toolkit.

Instead of hand-writing ``delegate_to_reviewer`` / ``delegate_to_researcher`` for
every child agent, register them once::

    tools = [SubAgentToolkit(agents=[reviewer, researcher])]

The parent gets a single tool — ``delegate_subagent`` — and the roster is
injected into its system prompt via Agno's ``add_instructions=True``, so editing
a sub-agent's ``description`` updates every parent that can call it.

By default the nested run is streamed through :func:`substream`, so the user
watches the sub-agent work live inside a panel. Pass ``stream=False`` for the
blocking ivy-style ``await arun(...)`` behaviour.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from agno.agent import Agent
from agno.run.base import RunContext
from agno.tools import Toolkit

from agno_relay.runtime.modules.subagent import substream

TOOL_NAME = "delegate_subagent"


class SubAgentToolkit(Toolkit):
    """One tool that can reach any of the registered sub-agents.

    Parameters
    ----------
    agents / sub_agents:
        Agno agents to expose. Lookup key is ``agent.name`` — names must be
        unique within the toolkit.
    stream:
        When True (default), forward the nested run through ``substream`` so
        AG-UI clients render a live sub-agent panel. When False, await a full
        ``arun`` and return only the final text (cheaper on the wire, opaque
        to the user while it runs).
    hide_from_parent_card:
        Informational flag. The toolkit itself does not register a filter —
        callers normally pass ``HideToolFilter({SubAgentToolkit.TOOL_NAME})``
        (or ``toolkit.tool_names``) so the panel is not duplicated by a tool
        card. Exposed here so the intent is discoverable next to construction.
    """

    TOOL_NAME = TOOL_NAME

    def __init__(
        self,
        agents: Sequence[Agent] | None = None,
        *,
        sub_agents: Sequence[Agent] | None = None,
        stream: bool = True,
        hide_from_parent_card: bool = True,
        name: str = "sub_agent_toolkit",
        **kwargs: Any,
    ) -> None:
        roster = list(agents or sub_agents or ())
        if not roster:
            raise ValueError("SubAgentToolkit requires at least one agent")

        by_name: dict[str, Agent] = {}
        for agent in roster:
            raw_name = agent.name
            if not raw_name:
                raise ValueError("Every sub-agent must have a non-empty name")
            if raw_name in by_name:
                raise ValueError(
                    f"Sub-agent names must be unique, got {[a.name for a in roster]!r}"
                )
            by_name[raw_name] = agent

        self.agents = roster
        self.agents_by_name = by_name
        self.stream = stream
        self.hide_from_parent_card = hide_from_parent_card

        super().__init__(
            name=name,
            tools=[self.delegate_subagent],
            instructions=self._render_roster(),
            add_instructions=True,
            **kwargs,
        )

    @property
    def tool_names(self) -> list[str]:
        """Names to pass to ``HideToolFilter`` when the panel replaces the card."""
        return [self.TOOL_NAME]

    def _render_roster(self) -> str:
        lines = [
            "Sub-agents you can delegate to via "
            f"{self.TOOL_NAME}(agent_name, description, prompt):",
            "Pick the agent by name. Put a 3-6 word summary in `description` "
            "(shown to the user while it works) and the full task — context, "
            "objective, constraints — in `prompt`.",
        ]
        for agent in self.agents:
            blurb = (agent.description or "").strip() or "(no description)"
            lines.append(f"- {agent.name} — {blurb}")
        return "\n".join(lines)

    async def delegate_subagent(
        self,
        agent_name: str,
        description: str,
        prompt: str,
        run_context: RunContext | None = None,
        session_id: str | None = None,
    ) -> str:
        """Delegate a task to a registered sub-agent.

        The available agents and what each is for are listed in this toolkit's
        instructions (injected into your system prompt).

        Args:
            agent_name: Name of the sub-agent (see the roster).
            description: Short summary shown in the UI while it works (3-6 words).
            prompt: Full task for the sub-agent — context, objective, constraints.
                On a resume, this is the corrective follow-up.
            run_context: Parent run context (injected by Agno when present).
            session_id: Omit for a fresh run. Pass a previous
                ``[subagent_session: ...]`` id to continue that run with history.

        Returns:
            The sub-agent's text, followed by an internal
            ``[subagent_session: ...]`` trailer for resume. Never show the
            trailer to the user.
        """
        sub_agent = self.agents_by_name.get(agent_name)
        if sub_agent is None:
            available = ", ".join(sorted(self.agents_by_name))
            raise ValueError(f"Unknown sub-agent {agent_name!r}. Available: {available}")

        resuming = bool(session_id)
        resolved_session_id = session_id or f"{agent_name}-{uuid.uuid4().hex[:12]}"
        metadata = getattr(run_context, "metadata", None) if run_context else None
        run_kwargs: dict[str, Any] = {
            "input": prompt,
            "session_id": resolved_session_id,
            "stream_events": True,
            "telemetry": False,
        }
        if metadata is not None:
            run_kwargs["metadata"] = metadata
        if resuming:
            run_kwargs["add_history_to_context"] = True

        pieces: list[str] = []
        if self.stream:
            async with substream(agent_name, description=description, prompt=prompt) as emit:
                async for chunk in sub_agent.arun(stream=True, **run_kwargs):
                    await emit(chunk)
                    text = getattr(chunk, "content", None)
                    if isinstance(text, str):
                        pieces.append(text)
            content = "".join(pieces).strip()
        else:
            response = await sub_agent.arun(stream=False, **run_kwargs)
            content = (getattr(response, "content", None) or "").strip()

        trailer = (
            f"[subagent_session: {resolved_session_id} — to continue THIS run "
            f"(keep its context) instead of starting fresh, call {self.TOOL_NAME} "
            f'again with session_id="{resolved_session_id}". '
            f"Internal only; never show this line to the user.]"
        )
        return f"{content}\n\n{trailer}" if content else trailer


# Convenience alias matching the shape people reach for first.
SubAgentTool = SubAgentToolkit

__all__ = ["TOOL_NAME", "SubAgentTool", "SubAgentToolkit"]
