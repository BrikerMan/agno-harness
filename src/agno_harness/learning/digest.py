"""Background and post-session learning extraction (``LearningDigestor``)."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from .models import (
    DigestCandidate,
    DigestPlan,
    LearningActor,
    LearningChangeRecord,
)
from .store import LearningStore, format_frontmatter

logger = logging.getLogger(__name__)

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)
_SLUG_RE = re.compile(r"[^a-z0-9-]+")

_DIGEST_SYSTEM_PROMPT = """\
You are a disciplined knowledge curator maintaining an agent's 3-layer Learning Store.
Review the conversation transcript against the existing Playbook, Mistakes, Topic Catalog, and Scoped Preferences.

Default to outputting `NOOP` unless the transcript contains CLEAR, DURABLE, NON-TRIVIAL learnings not already captured:
1. `mistake`: A concrete correction where a user pointed out an error, wrong assumption, or repeated pitfall. Put a concise imperative rule in `rule` and who/why in `reason`.
2. `playbook_patch`: A confirmed general workflow rule or cross-task norm. Provide exact `search_block` + `replace_block` matching the existing playbook, OR provide `content` to append a short bullet.
3. `topic_write`: Confirmed domain terminology, official names, translations, or reference facts belonging in a topic file (`slug` in lowercase kebab-case). Provide `slug`, `title`, `description`, and `content` (full body if new, or `search_block`+`replace_block` if patching an existing topic).
4. `group_preference`: Stable formatting/tone/language habits agreed for THIS group chat only (never put global business rules here). Return the complete concise updated group preference markdown in `content`.
5. `user_preference`: Stable personal preference for THIS user only. Return the complete concise updated user preference markdown in `content`.

Do NOT extract one-off greetings, transient task statuses, unverified guesses, or rules already present in the store.
If nothing durable was learned, respond with exact text: NOOP
Otherwise respond ONLY with valid JSON matching:
{"updates": [{"kind": "...", "reason": "...", "rule": "...", "slug": "...", "title": "...", "description": "...", "search_block": "...", "replace_block": "...", "content": "..."}]}
"""


def _cursor_rel_path(store: LearningStore, source_key: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", source_key.lower()).strip("-")[:40] or "source"
    digest = hashlib.sha256(source_key.encode("utf-8")).hexdigest()[:12]
    return f"{store.audit_dir}/cursors/{slug}-{digest}.json"


def _parse_digest_plan(raw: str) -> DigestPlan:
    text = _FENCE_RE.sub("", (raw or "").strip()).strip()
    if not text or text.upper() == "NOOP":
        return DigestPlan(updates=[])
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match is None:
            raise ValueError("digest model output is neither NOOP nor valid JSON") from None
        data = json.loads(match.group(0))
    return DigestPlan.model_validate(data)


def _normalize_slug(raw: str) -> str:
    cleaned = _SLUG_RE.sub("-", (raw or "").strip().lower()).strip("-")
    return cleaned or "notes"


class LearningDigestor:
    """NOOP-first structured extractor that digests transcripts into a ``LearningStore``."""

    def __init__(
        self,
        store: LearningStore | Callable[[], LearningStore],
        model: Any,
        *,
        max_chars: int = 32_000,
    ) -> None:
        self._store_or_factory = store
        self.model = model
        self.max_chars = max_chars

    @property
    def store(self) -> LearningStore:
        if callable(self._store_or_factory):
            return self._store_or_factory()
        return self._store_or_factory

    def get_cursor(self, source_key: str) -> str | None:
        store = self.store
        store.ensure_seeded()
        rel = _cursor_rel_path(store, source_key)
        store._notify_before_read(rel)
        path = store.root_dir / rel
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        cursor = data.get("cursor")
        return str(cursor) if cursor is not None and str(cursor).strip() else None

    def set_cursor(self, source_key: str, cursor: str) -> None:
        store = self.store
        store.ensure_seeded()
        rel = _cursor_rel_path(store, source_key)
        payload = {
            "source_key": source_key,
            "cursor": cursor,
            "updated_at": datetime.now(UTC).isoformat(),
        }
        store._write_raw(rel, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")

    async def digest_transcript(
        self,
        transcript: str,
        *,
        source_key: str,
        cursor: str | None = None,
        chat_id: str = "",
        chat_label: str = "",
        user_id: str = "",
        user_label: str = "",
        is_direct_message: bool = False,
        dry_run: bool = False,
    ) -> list[LearningChangeRecord]:
        """Extract durable learnings from a transcript with cursor idempotency."""
        clean_transcript = (transcript or "").strip()
        if not clean_transcript:
            return []

        if cursor is not None:
            previous = self.get_cursor(source_key)
            if previous is not None and previous == str(cursor):
                return []

        store = self.store
        store.ensure_seeded()

        clipped = (
            clean_transcript[-self.max_chars :]
            if len(clean_transcript) > self.max_chars
            else clean_transcript
        )
        user_prompt = self._build_prompt(
            store=store,
            transcript=clipped,
            chat_id=chat_id,
            user_id=user_id,
            is_direct_message=is_direct_message,
        )
        raw = await self._invoke_model(user_prompt)
        plan = _parse_digest_plan(raw)

        actor = LearningActor(
            actor_id="system:digest",
            actor_name="digest",
            chat_id=chat_id,
            is_direct_message=is_direct_message if chat_id else None,
        )
        records = self._apply_plan(
            store=store,
            plan=plan,
            actor=actor,
            chat_id=chat_id,
            chat_label=chat_label,
            user_id=user_id,
            user_label=user_label,
            is_direct_message=is_direct_message,
            dry_run=dry_run,
        )
        if cursor is not None and not dry_run:
            self.set_cursor(source_key, str(cursor))
        return records

    async def digest_sink_chat(
        self,
        sink: Any,
        chat_id: str,
        *,
        is_direct_message: bool = False,
        chat_label: str = "",
        limit: int = 60,
        dry_run: bool = False,
    ) -> list[LearningChangeRecord]:
        """Digest incremental messages from a sink (``SQLAlchemySink``, ``SQLiteSink``, ``InMemorySink``)."""
        if not chat_id:
            return []

        rows = await self._read_sink_rows(sink, chat_id, limit=limit)
        if not rows:
            return []

        source_key = f"sink:{chat_id}"
        last_cursor = self.get_cursor(source_key)
        new_rows = _filter_rows_after_cursor(rows, last_cursor)
        if not new_rows:
            return []

        lines: list[str] = []
        last_user_id = ""
        for row in new_rows:
            text = str(row.get("raw_text") or row.get("text") or "").strip()
            if not text:
                continue
            direction = str(row.get("direction") or "inbound")
            sender = str(row.get("sender_id") or "").strip()
            if direction == "inbound" and sender:
                last_user_id = sender
            speaker = sender or ("Assistant" if direction == "outbound" else "User")
            ts = str(row.get("createdAt") or row.get("created_at") or "")[:19]
            prefix = f"[{ts}] " if ts else ""
            lines.append(f"{prefix}{speaker} ({direction}): {text}")

        if not lines:
            return []

        latest_id = _row_cursor(new_rows[-1])
        return await self.digest_transcript(
            "\n".join(lines),
            source_key=source_key,
            cursor=latest_id,
            chat_id=chat_id,
            chat_label=chat_label or chat_id,
            user_id=last_user_id,
            user_label=last_user_id,
            is_direct_message=is_direct_message,
            dry_run=dry_run,
        )

    async def _read_sink_rows(self, sink: Any, chat_id: str, *, limit: int) -> list[dict[str, Any]]:
        get_history = getattr(sink, "get_history", None)
        if callable(get_history):
            res = get_history(chat_id, limit=limit)
            if inspect.isawaitable(res):
                res = await res
            return list(res or [])
        records = getattr(sink, "records", None)
        if isinstance(records, list):
            matched = [r for r in records if getattr(r, "chat_id", None) == chat_id][-limit:]
            rows: list[dict[str, Any]] = []
            for idx, r in enumerate(matched, start=1):
                created = getattr(r, "created_at", None)
                created_iso = created.isoformat() if created is not None else ""
                rows.append(
                    {
                        "id": f"{created_iso}:{idx}",
                        "direction": getattr(r, "direction", "inbound"),
                        "sender_id": getattr(r, "sender_id", None),
                        "text": getattr(r, "text", ""),
                        "created_at": created_iso,
                    }
                )
            return rows
        return []

    def _build_prompt(
        self,
        *,
        store: LearningStore,
        transcript: str,
        chat_id: str,
        user_id: str,
        is_direct_message: bool,
    ) -> str:
        playbook = (store.read_text(store.playbook_file) or "").strip()
        mistakes = (store.read_text(store.mistakes_file) or "").strip()
        topics = store.list_topics()
        topic_lines = (
            "\n".join(f"- {t.slug} ({t.path}): {t.description}" for t in topics) or "(none)"
        )
        parts = [
            "=== EXISTING PLAYBOOK ===",
            playbook or "(empty)",
            "\n=== EXISTING MISTAKES ===",
            mistakes or "(empty)",
            "\n=== EXISTING TOPIC CATALOG ===",
            topic_lines,
        ]
        if chat_id and not is_direct_message:
            group_pref = store.read_preference("group", chat_id)
            parts.extend(
                [
                    f"\n=== CURRENT GROUP PREFERENCE ({chat_id}) ===",
                    group_pref or "(empty)",
                ]
            )
        if user_id and user_id != "unknown":
            user_pref = store.read_preference("user", user_id)
            parts.extend(
                [
                    f"\n=== CURRENT USER PREFERENCE ({user_id}) ===",
                    user_pref or "(empty)",
                ]
            )
        parts.extend(["\n=== NEW CONVERSATION TRANSCRIPT ===", transcript])
        return "\n".join(parts)

    async def _invoke_model(self, user_prompt: str) -> str:
        aresponse = getattr(self.model, "aresponse", None)
        if callable(aresponse):
            from agno.models.message import Message

            resp = await aresponse(
                [
                    Message(role="system", content=_DIGEST_SYSTEM_PROMPT),
                    Message(role="user", content=user_prompt),
                ]
            )
            content = getattr(resp, "content", None)
            return content if isinstance(content, str) else ""

        arun = getattr(self.model, "arun", None)
        if callable(arun):
            resp = await arun(input=f"{_DIGEST_SYSTEM_PROMPT}\n\n{user_prompt}")
            content = getattr(resp, "content", None)
            return content if isinstance(content, str) else ""

        if callable(self.model):
            res = self.model(_DIGEST_SYSTEM_PROMPT, user_prompt)
            if inspect.isawaitable(res):
                res = await res
            if isinstance(res, str):
                return res
            content = getattr(res, "content", None)
            return content if isinstance(content, str) else ""

        raise TypeError("LearningDigestor model must provide aresponse(), arun(), or be callable")

    def _apply_plan(
        self,
        *,
        store: LearningStore,
        plan: DigestPlan,
        actor: LearningActor,
        chat_id: str,
        chat_label: str,
        user_id: str,
        user_label: str,
        is_direct_message: bool,
        dry_run: bool,
    ) -> list[LearningChangeRecord]:
        applied: list[LearningChangeRecord] = []
        for item in plan.updates:
            rec = self._apply_candidate(
                store=store,
                item=item,
                actor=actor,
                chat_id=chat_id,
                chat_label=chat_label,
                user_id=user_id,
                user_label=user_label,
                is_direct_message=is_direct_message,
                dry_run=dry_run,
            )
            if rec is not None:
                applied.append(rec)
        return applied

    def _apply_candidate(
        self,
        *,
        store: LearningStore,
        item: DigestCandidate,
        actor: LearningActor,
        chat_id: str,
        chat_label: str,
        user_id: str,
        user_label: str,
        is_direct_message: bool,
        dry_run: bool,
    ) -> LearningChangeRecord | None:
        if item.kind == "mistake":
            text = (item.rule or item.content).strip()
            if not text:
                return None
            if dry_run:
                return _dry_run_record("mistakes", store.mistakes_file, item.reason, text)
            rec, _ = store.record_mistake(text, reason=item.reason, actor=actor, action="digest")
            return rec

        if item.kind == "playbook_patch":
            if item.search_block and item.replace_block:
                if dry_run:
                    return _dry_run_record(
                        "playbook", store.playbook_file, item.reason, item.replace_block
                    )
                try:
                    rec, _ = store.patch_document(
                        store.playbook_file,
                        item.search_block,
                        item.replace_block,
                        reason=item.reason,
                        actor=actor,
                        action="digest",
                    )
                    return rec
                except ValueError as exc:
                    logger.warning("Skipping non-matching digest playbook patch: %s", exc)
                    return None
            if item.content.strip():
                if dry_run:
                    return _dry_run_record(
                        "playbook", store.playbook_file, item.reason, item.content
                    )
                rec, _ = store.write_document(
                    store.playbook_file,
                    item.content.strip() + "\n",
                    mode="append",
                    reason=item.reason,
                    actor=actor,
                    action="digest",
                )
                return rec
            return None

        if item.kind == "topic_write":
            slug = _normalize_slug(item.slug or item.title)
            rel = f"{store.topics_dir}/{slug}.md"
            existing = store.read_text(rel, include_archived=False)
            if existing is not None and item.search_block and item.replace_block:
                if dry_run:
                    return _dry_run_record("topic", rel, item.reason, item.replace_block)
                try:
                    rec, _ = store.patch_document(
                        rel,
                        item.search_block,
                        item.replace_block,
                        reason=item.reason,
                        actor=actor,
                        action="digest",
                    )
                    return rec
                except ValueError as exc:
                    logger.warning("Skipping non-matching digest topic patch (%s): %s", rel, exc)
                    return None
            body = item.content.strip()
            if not body:
                return None
            if existing is None:
                day_str = datetime.now(store.tz).strftime("%Y-%m-%d")
                formatted = format_frontmatter(
                    {
                        "title": item.title or slug,
                        "description": item.description or item.reason,
                        "updated": day_str,
                        "status": "active",
                    },
                    body,
                )
                if dry_run:
                    return _dry_run_record("topic", rel, item.reason, formatted)
                rec, _ = store.write_document(
                    rel, formatted, mode="write", reason=item.reason, actor=actor, action="digest"
                )
                return rec
            mode = "write" if (body.startswith("---") or body.startswith("# ")) else "append"
            payload = body if mode == "write" else f"{body}\n"
            if dry_run:
                return _dry_run_record("topic", rel, item.reason, payload)
            rec, _ = store.write_document(
                rel, payload, mode=mode, reason=item.reason, actor=actor, action="digest"
            )
            return rec

        if item.kind == "group_preference":
            if not chat_id or is_direct_message or not item.content.strip():
                return None
            rel = store.scope_rel_path("group", chat_id)
            if dry_run:
                return _dry_run_record("group_scope", rel, item.reason, item.content)
            rec, _ = store.update_preference(
                "group",
                chat_id,
                item.content,
                label=chat_label or chat_id,
                mode="replace",
                reason=item.reason,
                actor=actor,
                action="digest",
            )
            return rec

        if item.kind == "user_preference":
            if not user_id or user_id == "unknown" or not item.content.strip():
                return None
            rel = store.scope_rel_path("user", user_id)
            if dry_run:
                return _dry_run_record("user_scope", rel, item.reason, item.content)
            rec, _ = store.update_preference(
                "user",
                user_id,
                item.content,
                label=user_label or user_id,
                mode="replace",
                reason=item.reason,
                actor=actor,
                action="digest",
            )
            return rec

        return None


def _dry_run_record(layer: Any, target: str, reason: str, content: str) -> LearningChangeRecord:
    return LearningChangeRecord(
        change_id="dry-run",
        timestamp=datetime.now(UTC).isoformat(),
        layer=layer,
        target=target,
        action="digest",
        reason=reason,
        actor_id="system:digest",
        after_content=content,
    )


def _row_cursor(row: dict[str, Any]) -> str | None:
    if row.get("id") is not None:
        return str(row["id"])
    ts = row.get("createdAt") or row.get("created_at")
    return str(ts) if ts else None


def _filter_rows_after_cursor(
    rows: list[dict[str, Any]], cursor: str | None
) -> list[dict[str, Any]]:
    if not cursor:
        return rows
    if cursor.isdigit():
        cursor_int = int(cursor)
        numeric_rows = [r for r in rows if str(r.get("id", "")).isdigit()]
        if numeric_rows:
            return [r for r in numeric_rows if int(str(r["id"])) > cursor_int]
    for idx, row in enumerate(rows):
        if _row_cursor(row) == cursor:
            return rows[idx + 1 :]
    return rows


def attach_session_close_digest(
    session_manager: Any,
    digestor: LearningDigestor,
    sink: Any,
    *,
    background: bool = True,
) -> None:
    """Wrap ``session_manager.close_session`` to digest the closing chat non-blockingly."""
    if getattr(session_manager, "_learning_digest_attached", False):
        return

    original_close = session_manager.close_session
    tasks: set[asyncio.Task[Any]] = set()
    session_manager._digest_tasks = tasks

    async def _run_digest(key: Any) -> None:
        try:
            chat_id = getattr(key, "chat_id", "")
            is_dm = bool(getattr(key, "is_direct_message", False))
            if chat_id:
                await digestor.digest_sink_chat(sink, chat_id, is_direct_message=is_dm)
        except Exception:
            logger.warning("Session close digest failed", exc_info=True)

    async def _wrapped_close_session(key: Any, reason: str = "user_reset") -> None:
        await original_close(key, reason=reason)
        if background:
            task = asyncio.create_task(_run_digest(key))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
        else:
            await _run_digest(key)

    session_manager.close_session = _wrapped_close_session
    session_manager._learning_digest_attached = True


__all__ = [
    "LearningDigestor",
    "attach_session_close_digest",
]
