"""One prefix for every table an agent owns.

Omit the prefix and names start with ``agno-harness-``. Pass ``ipv-agent`` or
``admin-agent`` when several agents share one database.
"""

from __future__ import annotations

import re
from typing import Any

DEFAULT_TABLE_PREFIX = "agno-harness"

_PREFIX = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")

# Attribute on an Agno db object, and the suffix appended after the prefix.
_AGNO_TABLES: dict[str, str] = {
    "session_table_name": "sessions",
    "runs_table_name": "runs",
    "memory_table_name": "memories",
    "metrics_table_name": "metrics",
    "eval_table_name": "eval-runs",
    "knowledge_table_name": "knowledge",
    "trace_table_name": "traces",
    "span_table_name": "spans",
    "versions_table_name": "schema-versions",
    "components_table_name": "components",
    "component_configs_table_name": "component-configs",
    "component_links_table_name": "component-links",
    "learnings_table_name": "learnings",
    "schedules_table_name": "schedules",
    "schedule_runs_table_name": "schedule-runs",
    "job_table_name": "jobs",
    "tool_results_table_name": "tool-results",
    "approvals_table_name": "approvals",
    "auth_tokens_table_name": "auth-tokens",
    "service_accounts_table_name": "service-accounts",
    "mcp_oauth_clients_table_name": "mcp-oauth-clients",
    "mcp_oauth_transactions_table_name": "mcp-oauth-transactions",
    "mcp_oauth_codes_table_name": "mcp-oauth-codes",
    "mcp_oauth_refresh_tokens_table_name": "mcp-oauth-refresh-tokens",
    "mcp_oauth_keys_table_name": "mcp-oauth-keys",
}


def resolve_prefix(prefix: str | None = None) -> str:
    """Return the prefix to use. ``None`` selects :data:`DEFAULT_TABLE_PREFIX`."""
    chosen = (prefix if prefix is not None else DEFAULT_TABLE_PREFIX).strip().strip("-_")
    if not chosen or not _PREFIX.fullmatch(chosen):
        raise ValueError(
            "table prefix must start with a letter and contain only letters, digits, underscores, and hyphens"
        )
    return chosen


def table_name(suffix: str, *, prefix: str | None = None) -> str:
    """``agno-harness-sessions`` by default, or ``ipv_sessions`` / ``ipv-agent-sessions`` when prefixed."""
    p = resolve_prefix(prefix)
    if "-" in p:
        sep = "-"
        formatted_suffix = suffix.replace("_", "-")
    else:
        sep = "_"
        formatted_suffix = suffix.replace("-", "_")
    return f"{p}{sep}{formatted_suffix}"


def apply_table_prefix(db: Any, prefix: str | None = None) -> Any:
    """Rename every Agno table on ``db`` before the first query creates one.

    Attributes the database object does not have are left alone, so SQLite and
    Postgres builds can share this call.
    """
    for attr, suffix in _AGNO_TABLES.items():
        if hasattr(db, attr):
            setattr(db, attr, table_name(suffix, prefix=prefix))
    return db


__all__ = [
    "DEFAULT_TABLE_PREFIX",
    "apply_table_prefix",
    "resolve_prefix",
    "table_name",
]
