"""Pydantic models and actor resolution for ``agno_harness.learning``."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..runtime.scope import current_scope

LearningLayer = Literal[
    "playbook",
    "mistakes",
    "topic",
    "user_scope",
    "group_scope",
    "system",
]

LearningAction = Literal[
    "seed",
    "create",
    "write",
    "patch",
    "append",
    "archive",
    "revert",
    "digest",
    "repair",
    "external_sync",
]


class LearningActor(BaseModel):
    """Identity and channel origin for a learning mutation or lookup."""

    model_config = ConfigDict(extra="forbid")

    actor_id: str = "unknown"
    actor_name: str = ""
    platform: str = ""
    chat_id: str = ""
    session_id: str = ""
    is_direct_message: bool | None = None


def resolve_learning_actor(run_context: Any = None) -> LearningActor:
    """Resolve actor and chat context from ``RunContext`` and ambient ``RunScope``."""
    scope = current_scope()
    merged_meta: dict[str, Any] = {}
    if scope is not None and isinstance(getattr(scope, "metadata", None), dict):
        merged_meta.update(scope.metadata)
    if run_context is not None and isinstance(getattr(run_context, "metadata", None), dict):
        merged_meta.update(run_context.metadata)

    raw_actor_id = (
        merged_meta.get("sender_id")
        or (getattr(run_context, "user_id", None) if run_context is not None else None)
        or (getattr(scope, "user_id", None) if scope is not None else None)
        or "unknown"
    )
    actor_id = str(raw_actor_id).strip() or "unknown"
    actor_name = str(merged_meta.get("sender_name") or "").strip()
    platform = str(merged_meta.get("platform") or "").strip()
    chat_id = str(merged_meta.get("chat_id") or "").strip()
    session_id = str(
        (getattr(run_context, "session_id", None) if run_context is not None else None)
        or (getattr(scope, "thread_id", None) if scope is not None else None)
        or ""
    ).strip()
    raw_dm = merged_meta.get("is_direct_message")
    is_direct_message = raw_dm if isinstance(raw_dm, bool) else None

    return LearningActor(
        actor_id=actor_id,
        actor_name=actor_name,
        platform=platform,
        chat_id=chat_id,
        session_id=session_id,
        is_direct_message=is_direct_message,
    )


class TopicCatalogEntry(BaseModel):
    """Metadata summary for one Layer 2 topic/glossary file."""

    model_config = ConfigDict(extra="forbid")

    slug: str
    path: str
    title: str
    description: str
    updated: str = ""
    update_rules: str = ""


class LearningChangeRecord(BaseModel):
    """Immutable audit snapshot for a single learning mutation."""

    model_config = ConfigDict(extra="forbid")

    change_id: str
    timestamp: str
    layer: LearningLayer
    target: str
    action: LearningAction
    reason: str
    actor_id: str
    actor_name: str = ""
    platform: str = ""
    chat_id: str = ""
    session_id: str = ""
    diff: str = ""
    before_content: str | None = None
    after_content: str | None = None
    after_sha256: str = ""
    reverted_change_id: str | None = None


class DoctorIssue(BaseModel):
    """One integrity, missing-file, or external-drift issue detected by ``doctor()``."""

    model_config = ConfigDict(extra="forbid")

    path: str
    kind: Literal["missing", "corrupted", "external_drift"]
    detail: str
    repaired: bool = False
    action_taken: str = ""


class DoctorReport(BaseModel):
    """Summary returned by ``LearningStore.doctor()``."""

    model_config = ConfigDict(extra="forbid")

    ok: bool
    checked_files: int
    issues: list[DoctorIssue] = Field(default_factory=list)


class DigestCandidate(BaseModel):
    """One durable learning candidate extracted from conversation or chat history."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal[
        "mistake",
        "playbook_patch",
        "topic_write",
        "user_preference",
        "group_preference",
    ]
    reason: str = "extracted from conversation"
    rule: str = ""
    slug: str = ""
    title: str = ""
    description: str = ""
    search_block: str = ""
    replace_block: str = ""
    content: str = ""


class DigestPlan(BaseModel):
    """Structured output returned by ``LearningDigestor``."""

    model_config = ConfigDict(extra="forbid")

    updates: list[DigestCandidate] = Field(default_factory=list)


__all__ = [
    "DigestCandidate",
    "DigestPlan",
    "DoctorIssue",
    "DoctorReport",
    "LearningAction",
    "LearningActor",
    "LearningChangeRecord",
    "LearningLayer",
    "TopicCatalogEntry",
    "resolve_learning_actor",
]
