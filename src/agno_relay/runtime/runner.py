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

from collections.abc import AsyncIterator
from typing import Any

from ag_ui.core import RunAgentInput
from agno.os.interfaces.agui.input import extract_context, extract_media, extract_user_input
from agno.run.base import RunContext

from .hitl import client_tool_functions, detect_resume, resume_paused_run
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
    """

    def __init__(self, agent: Any, *, enable_subagent_streaming: bool = True) -> None:
        self.agent = agent
        self.enable_subagent_streaming = enable_subagent_streaming

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
            # different Context).
            with scope.enter_step(), bind_channel(bound), bind_scope(scope):
                try:
                    item = await merged.__anext__()
                except StopAsyncIteration:
                    break
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


__all__ = ["AgentRunner"]
