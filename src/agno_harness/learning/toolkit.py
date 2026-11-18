"""Agno Toolkit for 3-layer continuous learning, scope isolation, and rollback."""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, cast

from agno.run import RunContext
from agno.tools.toolkit import Toolkit

from .models import LearningLayer, resolve_learning_actor
from .store import LearningStore

_VALID_LAYERS: frozenset[str] = frozenset(
    {"playbook", "mistakes", "topic", "user_scope", "group_scope", "system"}
)


class LearningToolkit(Toolkit):
    """Structured read/write/audit toolkit over a :class:`LearningStore`."""

    def __init__(
        self,
        store: LearningStore | Callable[[], LearningStore],
        *,
        allow_write: bool = True,
        include_file_tools: bool = True,
        max_entries: int = 100,
        max_matches: int = 50,
        name: str = "learning",
        instructions: str | None = None,
        add_instructions: bool = True,
        **toolkit_kwargs: Any,
    ) -> None:
        self._store_or_factory = store
        self.allow_write = allow_write
        self.include_file_tools = include_file_tools
        self.max_entries = max_entries
        self.max_matches = max_matches

        instruction_text = instructions or self._default_instructions()
        super().__init__(
            name=name,
            instructions=instruction_text,
            add_instructions=add_instructions,
            **toolkit_kwargs,
        )

        if self.include_file_tools:
            self.register(self.read, name="read")
            self.register(self.grep, name="grep")
            self.register(self.list_files, name="list_files")
        self.register(self.list_learnings, name="list_learnings")

        if self.allow_write:
            self.register(self.record_mistake, name="record_mistake")
            if self.include_file_tools:
                self.register(self.patch_file, name="patch_file")
                self.register(self.write_file, name="write_file")
            self.register(self.update_preference, name="update_preference")
            self.register(self.revert_learning, name="revert_learning")
            self.register(self.repair_learning, name="repair_learning")

    @property
    def store(self) -> LearningStore:
        if callable(self._store_or_factory):
            return self._store_or_factory()
        return self._store_or_factory

    def _default_instructions(self) -> str:
        s = self.store
        lines = [
            "## Learning & Living Playbook Workspace",
            f"The live `{s.playbook_file}`, active `{s.mistakes_file}`, `{s.topics_dir}/` catalog, and active preferences are injected dynamically into your instructions every turn.",
            f"- Use `read(path=...)` or `grep(pattern=...)` to inspect `{s.topics_dir}/<slug>.md` or `{s.changelog_file}` on demand.",
            "- Use `list_files()` to browse non-internal topic/playbook files.",
            "- Use `list_learnings(limit=10)` when asked what you recently learned or changed.",
        ]
        if self.allow_write:
            lines.extend(
                [
                    "### Persistence SOP (How to Retain What Users Teach You)",
                    f"- **User correction / 'don't repeat X'**: Call `record_mistake(rule=..., reason=...)` immediately (and also `patch_file` on `{s.playbook_file}` if it modifies a standing workflow rule).",
                    f"- **Standing rule / guideline / human-edited draft diff**: Persist generalized rules to `{s.playbook_file}` via `patch_file` (or `write_file`).",
                    f"- **Domain terminology / glossary / topic knowledge**: Persist to `{s.topics_dir}/<slug>.md` via `patch_file` or `write_file`.",
                    "- **Chat-room or personal style/format preference**: Call `update_preference(scope='group'|'user', content=..., reason=...)` — never write private chat preferences into the global playbook.",
                    "- **Rollback / repair**: Use `revert_learning(change_id=..., reason=...)` to undo a learning, or `repair_learning()` if a file was corrupted.",
                    "- **Anti-Hallucination Rule**: NEVER claim '记住了 / Updated / Logged' unless the write tool returned success in the current turn. Always mention the exact layer/file updated.",
                ]
            )
        else:
            lines.append("NOTE: Write operations are disabled in this read-only mode.")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Read tools
    # ------------------------------------------------------------------

    def read(self, path: str = "", offset: int = 1, limit: int = 200) -> str:
        """Read a learning file slice or list a visible directory."""
        store = self.store
        store.ensure_seeded()
        root = store.root_dir
        clean = (path or "").strip()

        if not clean or clean == ".":
            return self._list_dir(store, root, ".")

        try:
            norm = store.normalize_rel_path(clean)
        except ValueError as exc:
            return f"Error: Security error: {exc}"

        if store.is_internal_path(norm):
            return f"Error: Security error: access to internal path '{path}' is restricted."

        target = root / norm
        if target.is_dir():
            return self._list_dir(store, target, norm)

        content = store.read_text(norm, include_archived=False)
        if content is None:
            return f"Error: Path '{path}' not found in learning store."

        lines = content.splitlines()
        total = len(lines)
        start_idx = max(0, offset - 1)
        end_idx = min(start_idx + limit, total)
        slice_lines = lines[start_idx:end_idx]
        end_line_num = min(offset + limit - 1, total)
        return f"--- {norm} (lines {offset} to {end_line_num} of {total}) ---\n" + "\n".join(
            slice_lines
        )

    def _list_dir(self, store: LearningStore, target: Path, display_dir: str) -> str:
        root = store.root_dir
        entries: list[str] = []
        try:
            children = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except OSError as exc:
            return f"Error reading directory '{display_dir}': {exc}"

        for child in children:
            if child.name.startswith("."):
                continue
            try:
                rel_child = child.relative_to(root).as_posix()
            except ValueError:
                continue
            if store.is_internal_path(rel_child):
                continue
            if (
                child.is_file()
                and child.suffix.lower() == ".md"
                and store.read_text(rel_child, include_archived=False) is None
            ):
                continue
            kind = "dir" if child.is_dir() else "file"
            size_suffix = f" ({child.stat().st_size} bytes)" if child.is_file() else ""
            entries.append(f"  [{kind}] {rel_child}{size_suffix}")
            if self.max_entries is not None and len(entries) >= self.max_entries:
                entries.append("  ... (results truncated)")
                break

        return f"Directory listing for '{display_dir}' ({len(entries)} items):\n" + "\n".join(
            entries
        )

    def grep(self, pattern: str, path: str = ".", is_regexp: bool = False) -> str:
        """Search for text or regex across visible learning files."""
        store = self.store
        store.ensure_seeded()
        root = store.root_dir
        clean = (path or ".").strip()

        rel_files: list[str] = []
        if not clean or clean == ".":
            for p in sorted(root.rglob("*")):
                if not p.is_file() or p.name.startswith("."):
                    continue
                rel = p.relative_to(root).as_posix()
                if store.is_internal_path(rel):
                    continue
                rel_files.append(rel)
        else:
            try:
                norm = store.normalize_rel_path(clean)
            except ValueError as exc:
                return f"Error: Security error: {exc}"
            if store.is_internal_path(norm):
                return f"Error: Security error: access to internal path '{path}' is restricted."
            target = root / norm
            if not target.exists():
                return f"Error: Path '{path}' not found."
            if target.is_file():
                rel_files = [norm]
            else:
                for p in sorted(target.rglob("*")):
                    if not p.is_file() or p.name.startswith("."):
                        continue
                    rel = p.relative_to(root).as_posix()
                    if not store.is_internal_path(rel):
                        rel_files.append(rel)

        regex = None
        if is_regexp:
            try:
                regex = re.compile(pattern)
            except re.error as exc:
                return f"Invalid regex pattern '{pattern}': {exc}"

        matches: list[str] = []
        truncated = False
        for rel in rel_files:
            content = store.read_text(rel, include_archived=False)
            if content is None:
                continue
            for line_no, line in enumerate(content.splitlines(), start=1):
                hit = bool(regex.search(line)) if regex else (pattern in line)
                if hit:
                    matches.append(f"  {rel}:{line_no}: {line}")
                    if self.max_matches is not None and len(matches) >= self.max_matches:
                        truncated = True
                        break
            if truncated:
                break

        lines = [f"Found {len(matches)} match(es) for '{pattern}':", *matches]
        if truncated:
            lines.append(f"  ... (results capped at {self.max_matches})")
        return "\n".join(lines)

    def list_files(self, pattern: str = "**/*") -> str:
        """List visible files in the learning store matching ``pattern``."""
        store = self.store
        store.ensure_seeded()
        root = store.root_dir
        try:
            candidates = sorted(root.glob(pattern))
        except Exception as exc:
            return f"Error listing learning files: {exc}"

        files: list[str] = []
        for p in candidates:
            if not p.is_file() or p.name.startswith("."):
                continue
            try:
                rel = p.relative_to(root).as_posix()
            except ValueError:
                continue
            if store.is_internal_path(rel):
                continue
            if p.suffix.lower() == ".md" and store.read_text(rel, include_archived=False) is None:
                continue
            files.append(rel)

        if not files:
            return f"No files matching '{pattern}' found in learning store."

        lines = [f"Available files in learning store ({len(files)} total):"]
        for f in files[: self.max_matches or len(files)]:
            lines.append(f"  - `{f}`")
        if self.max_matches is not None and len(files) > self.max_matches:
            lines.append(f"  ... ({len(files) - self.max_matches} more files)")
        return "\n".join(lines)

    def list_learnings(
        self,
        limit: int = 10,
        layer: str = "",
        run_context: RunContext | None = None,
    ) -> str:
        """Show recent audited learning changes visible to the current caller."""
        clean_layer = layer.strip()
        if clean_layer and clean_layer not in _VALID_LAYERS:
            return f"Error: layer must be one of {sorted(_VALID_LAYERS)}."
        actor = resolve_learning_actor(run_context)
        records = self.store.list_changes(
            limit=max(1, min(limit, 50)),
            layer=cast(LearningLayer, clean_layer) if clean_layer else None,
            actor=actor,
        )
        if not records:
            return "No recent learning changes recorded yet."
        lines = [f"Recent learning changes ({len(records)}):"]
        for rec in records:
            who = rec.actor_name or rec.actor_id
            lines.append(
                f"- `{rec.change_id}` [{rec.timestamp[:19]}] ({rec.layer} / {rec.action}) "
                f"`{rec.target}` — {rec.reason} (by {who})"
            )
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Write & maintenance tools (registered only when allow_write=True)
    # ------------------------------------------------------------------

    def record_mistake(
        self,
        rule: str,
        reason: str,
        run_context: RunContext | None = None,
    ) -> str:
        """Record a user correction or recurring pitfall into ``mistakes.md`` (newest first)."""
        if not self.allow_write:
            return "Error: Write operations are disabled in read-only mode."
        actor = resolve_learning_actor(run_context)
        try:
            _, msg = self.store.record_mistake(rule=rule, reason=reason, actor=actor)
            return msg
        except Exception as exc:
            return f"Error recording mistake: {exc}"

    def write_file(
        self,
        path: str,
        content: str,
        mode: str = "write",
        reason: str = "direct file write",
        run_context: RunContext | None = None,
    ) -> str:
        """Write or append to a playbook or topic/glossary file with atomic audit tracking."""
        if not self.allow_write:
            return "Error: Write operations are disabled in read-only mode."
        actor = resolve_learning_actor(run_context)
        try:
            _, msg = self.store.write_document(
                path,
                content,
                mode=mode,
                reason=reason,
                actor=actor,
            )
            return msg
        except Exception as exc:
            return f"Error writing to '{path}': {exc}"

    def patch_file(
        self,
        path: str,
        search_block: str,
        replace_block: str,
        reason: str = "direct file patch",
        run_context: RunContext | None = None,
    ) -> str:
        """Replace a unique block of text in a playbook, mistakes, or topic file."""
        if not self.allow_write:
            return "Error: Write operations are disabled in read-only mode."
        actor = resolve_learning_actor(run_context)
        try:
            _, msg = self.store.patch_document(
                path,
                search_block,
                replace_block,
                reason=reason,
                actor=actor,
            )
            return msg
        except Exception as exc:
            return f"Error patching '{path}': {exc}"

    def update_preference(
        self,
        scope: str,
        content: str,
        reason: str,
        mode: str = "replace",
        run_context: RunContext | None = None,
    ) -> str:
        """Update the caller's ``user`` preference or current chat's ``group`` preference."""
        if not self.allow_write:
            return "Error: Write operations are disabled in read-only mode."
        if scope not in {"user", "group"}:
            return "Error: scope must be 'user' or 'group'."
        actor = resolve_learning_actor(run_context)
        if scope == "group":
            if actor.is_direct_message is True:
                return "Error: Group preference is not available in direct messages."
            if not actor.chat_id:
                return "Error: Cannot determine group chat_id from current run context."
            scope_key = actor.chat_id
            label = actor.chat_id
        else:
            if not actor.actor_id or actor.actor_id == "unknown":
                return "Error: Cannot determine user identity from current run context."
            scope_key = actor.actor_id
            label = actor.actor_name or actor.actor_id

        try:
            _, msg = self.store.update_preference(
                cast(Literal["user", "group"], scope),
                scope_key,
                content,
                label=label,
                mode=mode,
                reason=reason,
                actor=actor,
            )
            return msg
        except Exception as exc:
            return f"Error updating {scope} preference: {exc}"

    def revert_learning(
        self,
        change_id: str,
        reason: str,
        run_context: RunContext | None = None,
    ) -> str:
        """Roll back a specific audited learning change by ``change_id``."""
        if not self.allow_write:
            return "Error: Write operations are disabled in read-only mode."
        actor = resolve_learning_actor(run_context)
        try:
            rec = self.store.revert(change_id, reason=reason, actor=actor)
            return f"Reverted '{change_id}' on '{rec.target}' (new change: {rec.change_id})."
        except Exception as exc:
            return f"Error reverting '{change_id}': {exc}"

    def repair_learning(
        self,
        restore_external_drift: bool = False,
        run_context: RunContext | None = None,
    ) -> str:
        """Run self-healing doctor on the learning store to fix missing or corrupted files."""
        if not self.allow_write:
            return "Error: Write operations are disabled in read-only mode."
        actor = resolve_learning_actor(run_context)
        report = self.store.doctor(
            repair=True,
            restore_untracked_drift=restore_external_drift,
            actor=actor,
        )
        if not report.issues:
            return f"Learning store is healthy ({report.checked_files} files checked, 0 issues)."
        summary = [
            f"- {item.path} ({item.kind}): {item.action_taken or item.detail}"
            for item in report.issues
        ]
        status = "repaired" if report.ok else "partially repaired"
        return f"Learning store {status} ({len(report.issues)} issue(s)):\n" + "\n".join(summary)


__all__ = ["LearningToolkit"]
