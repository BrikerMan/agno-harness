"""Runtime for the main agent.

Extend
------
build_relay() is what the process starts.
The runtime wraps only the coordinator. Helpers are tools on that agent.
Hide delegate_subagent and load_skill so the user sees the panel and the answer.
"""

from agno.db.sqlite import SqliteDb

from agno_harness import (
    AgentRuntime,
    HideToolFilter,
    RelayApp,
    SessionManager,
    SQLiteSessionStore,
    SubAgentToolkit,
)
from app.agents.main.agent import build as build_main
from app.agents.main.cards import CARD_CATALOG
from app.agents.main.tools.background_task import bind as bind_background_task
from app.paths import AGENT_DB, ensure_data


def build_relay() -> RelayApp:
    ensure_data()
    db = SqliteDb(db_file=str(AGENT_DB))
    runtime = AgentRuntime(
        agent=build_main(db),
        db=db,
        catalog=CARD_CATALOG,
        enable_subagent_streaming=True,
    )
    runtime.register_tool_filter(HideToolFilter({SubAgentToolkit.TOOL_NAME, "load_skill"}))
    relay = RelayApp(
        runtime=runtime,
        session_manager=SessionManager(store=SQLiteSessionStore(str(AGENT_DB))),
    )
    bind_background_task(relay)
    return relay
