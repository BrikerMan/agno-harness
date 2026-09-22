"""AgentRunner — start an Agno run and yield what comes out of it.

The other half of the old bridge. Where the translator turns chunks into events,
this produces the chunks: it builds Agno's ``RunContext`` from the scope, decides
between a fresh run and a HITL resume, and merges any sub-agent's chunks into the
parent's stream.

Splitting it out is what makes the two interesting non-default paths possible. A
caller who wants a different execution strategy — a queue, a retry, a replay of
recorded chunks — keeps the translator and replaces this. A caller who wants a
different event pipeline keeps this and replaces the translator.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from ag_ui.core import RunAgentInput
from agno.os.interfaces.agui.input import extract_context, extract_media, extract_user_input
from agno.run.base import RunContext

from ..core.prompt import (
    QueryTurn,
    UserQueryBuilder,
    default_builder,
    is_user_query_envelope,
    parse_sent_at,
)
from .hitl import client_tool_functions, detect_resume, resume_paused_run
from .replay import last_user_text
from .scope import RunScope, bind_scope
from .sidechannel import SideChannel, bind_channel, merge_side_channel


class AgentRunner:
    """Drives ``agent.arun()`` (or a resume) for one scope at a time.

    Parameters
    ----------
    agent:
        Any Agno ``Agent`` or ``Team``.
    enable_subagent_streaming:
        Let tools forward a sub-agent's chunks into this run via ``substream``.
        With it off, no side channel is bound and ``substream`` is a no-op.
    user_query_builder:
        Override the default :class:`UserQueryBuilder`. Wrapping the latest
        user turn as ``<user-query>`` plus ``<context>`` is always on.
    """

    def __init__(
        self,
        agent: Any,
        *,
        enable_subagent_streaming: bool = True,
        user_query_builder: UserQueryBuilder | None = None,
        read_timeout: float | None = 300.0,
    ) -> None:
        self.agent = agent
        self.enable_subagent_streaming = enable_subagent_streaming
        self.user_query_builder = user_query_builder or default_builder()
        self.read_timeout = read_timeout

    def build_run_context(self, run_input: RunAgentInput, scope: RunScope) -> RunContext:
        """Assemble Agno's run context from the scope.

        Called before the run so pre-run hooks have already had their say: the
        ``user_id``, the session state and the extra ``arun`` keyword arguments
        all come off the scope rather than from the request.
        """
        run_context = RunContext(
            run_id=scope.run_id,
            session_id=scope.thread_id,
            user_id=scope.user_id,
            client_tools=client_tool_functions(run_input),
            dependencies=extract_context(run_input.context),
            session_state=scope.session_state,
        )
        if run_context.dependencies:
            scope.run_kwargs.setdefault("add_dependencies_to_context", True)
        return run_context

    async def run(self, run_input: RunAgentInput, scope: RunScope) -> AsyncIterator[Any]:
        """Yield the run's chunks, with sub-agent signals merged in.

        Items are Agno chunks, ``ParentChunk`` wrappers, or ``SubAgentItem``
        signals — all of which :meth:`EventTranslator.feed` understands.
        """
        run_context = self.build_run_context(run_input, scope)
        scope.run_context = run_context

        with scope.enter_step():
            response_stream = await self._open(run_input, scope, run_context)

        channel = SideChannel(enabled=self.enable_subagent_streaming)
        bound = channel if self.enable_subagent_streaming else None
        merged = merge_side_channel(response_stream, channel).__aiter__()
        while True:
            # Bind the side channel and scope only while waiting on the agent/tools — never
            # across ``yield`` to the HTTP consumer. Holding a ContextVar across
            # an async-gen yield breaks on client disconnect (Token created in a
            # different Context). ``enter_step`` is *not* here: the agent runs
            # in the parent task ``merge_side_channel`` spawns, and that task
            # enters the step around ``__anext__`` so LLM / tool / sub-agent
            # spans nest under ``Agent.arun``.
            with bind_channel(bound), bind_scope(scope):
                try:
                    if self.read_timeout is not None:
                        item = await asyncio.wait_for(merged.__anext__(), timeout=self.read_timeout)
                    else:
                        item = await merged.__anext__()
                except StopAsyncIteration:
                    break
                except TimeoutError as exc:
                    raise TimeoutError(
                        f"Upstream model stream timed out after {self.read_timeout}s of inactivity"
                    ) from exc
            yield item

    async def _open(
        self,
        run_input: RunAgentInput,
        scope: RunScope,
        run_context: RunContext,
    ) -> Any:
        resume = detect_resume(run_input)
        if resume is not None:
            return await resume_paused_run(
                entity=self.agent,
                session_id=scope.thread_id,
                tool_messages=resume.tool_messages,
                run_context=run_context,
                run_kwargs=scope.run_kwargs,
            )

        messages = run_input.messages or []
        await self._wrap_user_query(messages, scope)
        images, audio, videos, files = extract_media(messages)
        return self.agent.arun(
            input=extract_user_input(messages),
            stream=True,
            stream_events=True,
            session_id=scope.thread_id,
            user_id=scope.user_id,
            run_id=scope.run_id,
            images=images or None,
            audio=audio or None,
            videos=videos or None,
            files=files or None,
            run_context=run_context,
            **scope.run_kwargs,
        )

    async def _wrap_user_query(self, messages: Any, scope: RunScope) -> None:
        raw = last_user_text(messages)
        if not raw or is_user_query_envelope(raw):
            return
        meta = scope.metadata
        sent_at = parse_sent_at(meta.get("sent_at")) or datetime.now(UTC)
        extras: dict[str, Any] = {}
        attachments = meta.get("attachments_text")
        if attachments:
            extras["attachments_text"] = attachments
        envelope = await self.user_query_builder.build(
            QueryTurn(
                text=raw,
                sent_at=sent_at,
                sender=meta.get("sender_name") or meta.get("sender_id") or scope.user_id,
                platform=meta.get("platform"),
                is_direct_message=meta.get("is_direct_message"),
                extras=extras,
            )
        )
        _set_last_user_content(messages, envelope.agent_input)
        scope.metadata["agent_input"] = envelope.agent_input


def _set_last_user_content(messages: Any, content: str) -> None:
    for item in reversed(list(messages or [])):
        role = getattr(item, "role", None)
        if role is None and isinstance(item, dict):
            role = item.get("role")
        if str(role) != "user":
            continue
        if isinstance(item, dict):
            item["content"] = content
        else:
            item.content = content
        return


__all__ = ["AgentRunner"]
