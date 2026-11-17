"""Pure local-directory 3-layer learning store with atomic audit & self-healing."""

from __future__ import annotations

import difflib
import hashlib
import json
import logging
import os
import re
import threading
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..tools.workspace import is_credential_path
from .models import (
    DoctorIssue,
    DoctorReport,
    LearningAction,
    LearningActor,
    LearningChangeRecord,
    LearningLayer,
    TopicCatalogEntry,
    resolve_learning_actor,
)

try:
    import yaml  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover - optional dependency
    yaml = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

_FRONTMATTER_RE = re.compile(r"^\ufeff?---\r?\n(.*?)\r?\n---(?:\r?\n(.*))?$", re.DOTALL)
_CHANGELOG_MARKER = "<!-- entries -->"
_MISTAKES_MARKER = "<!-- newest-first -->"
_SLUG_CLEAN_RE = re.compile(r"[^a-z0-9]+")
_MISTAKE_PREFIX_RE = re.compile(r"^[-*]\s+(?:\[\d{4}-\d{2}-\d{2}\]\s+)?")

_DEFAULT_PLAYBOOK = """\
# Agent Playbook & Operating Rules

> Standing rules, domain norms, and learned lessons injected on every turn.
> Follow these rules unless overridden by an explicit instruction in the current turn.

## 1. Core Principles
- Verify facts with tools before asserting them.
- Keep answers clear, direct, and grounded in recorded rules.
"""

_DEFAULT_MISTAKES = f"""\
# Common Mistakes & Corrections

> Standing corrections learned from user feedback (newest first).
> Never repeat a mistake recorded below.

{_MISTAKES_MARKER}
"""

_ROOT_README_TEMPLATE = """\
# Learning & Playbook Directory

- **Owner Agent**: `{owner_agent}`
- **Purpose**: Structured long-term memory and standing operating guidelines for `{owner_agent}`.

## Layout & Rules for Other Agents
- `{playbook_file}` & `{mistakes_file}`: Standing rules and past mistake corrections. Other agents may **read and cite** these as `{owner_agent}`'s operating norms.
- `{topics_dir}/`: Topic notes and glossaries. Safe for other agents to **read and cite**.
- `{scopes_dir}/`: Per-user and per-group conversation preferences for `{owner_agent}`. **DO NOT** read as universal truth or cite across chats.
- `{audit_dir}/`: Immutable audit snapshots and digest watermarks. **DO NOT** edit or delete.
- `{changelog_file}`: Human-readable summary of global rule updates.

> ⚠️ **Important**: Only `{owner_agent}` (or an explicit operator) should write to this directory. Do not overwrite or delete files here directly from another agent.
"""

_TOPICS_README_TEMPLATE = """\
# Topics & Glossaries (`{topics_dir}/`)

- **Owner Agent**: `{owner_agent}`
- **Access**: Safe for other agents to **read/cite**; edits should go through `{owner_agent}` so audit snapshots stay accurate.

Each `.md` file here represents one topic note or glossary indexed automatically into `{owner_agent}`'s catalog.
"""

_SCOPES_README_TEMPLATE = """\
# Scoped Preferences (`{scopes_dir}/`)

- **Owner Agent**: `{owner_agent}`
- **Access**: **INTERNAL ONLY — DO NOT CITE OR MODIFY** from other agents.

Files under `users/` and `groups/` store local communication style and formatting preferences for individual users or chat rooms. They are NOT shared business facts.
"""

_AUDIT_README_TEMPLATE = """\
# Audit & Self-Healing (`{audit_dir}/`)

- **Owner Agent**: `{owner_agent}`
- **Access**: **INTERNAL ONLY — DO NOT EDIT OR DELETE**.

- `changes/`: Immutable JSON snapshots (`before_content`, `after_content`, `after_sha256`, `diff`, `actor`, `reason`) used for traceability, rollback (`revert`), and corruption recovery (`doctor`).
- `cursors/`: Watermark cursors for background conversation/chat digesting.
"""

_CHANGELOG_HEADER = f"""\
# Learning Change Log

> Auto-maintained by `agno-harness` LearningStore. Newest entries appear at the top.

{_CHANGELOG_MARKER}
"""


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _resolve_tz(name: str) -> ZoneInfo:
    if name:
        try:
            return ZoneInfo(name)
        except ZoneInfoNotFoundError:
            pass
    return ZoneInfo("UTC")


def _fallback_parse_frontmatter(text: str) -> tuple[dict[str, Any], str | None]:
    """Minimal stdlib YAML mapping parser when PyYAML is unavailable."""
    result: dict[str, Any] = {}
    current_list_key: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("- ") and current_list_key is not None:
            item_val = stripped[2:].strip().strip('"').strip("'")
            result[current_list_key].append(item_val)
            continue
        if ":" not in line:
            return {}, f"invalid frontmatter line: {stripped!r}"
        current_list_key = None
        key, val = line.split(":", 1)
        clean_key = key.strip()
        if not clean_key:
            return {}, "empty frontmatter key"
        clean_val = val.strip()
        if not clean_val:
            result[clean_key] = []
            current_list_key = clean_key
        elif clean_val.startswith("[") and clean_val.endswith("]"):
            inner = clean_val[1:-1].strip()
            result[clean_key] = [
                item.strip().strip('"').strip("'") for item in inner.split(",") if item.strip()
            ]
        else:
            result[clean_key] = clean_val.strip('"').strip("'")
    return result, None


def split_frontmatter(text: str) -> tuple[dict[str, Any], str, str | None]:
    """Extract optional YAML frontmatter, body, and an optional parse error."""
    cleaned = text.lstrip("\ufeff")
    if cleaned.startswith("---\n") or cleaned.startswith("---\r\n") or cleaned.strip() == "---":
        match = _FRONTMATTER_RE.match(cleaned)
        if not match:
            return {}, cleaned.strip(), "unclosed YAML frontmatter"
        raw_yaml = match.group(1)
        body = (match.group(2) or "").strip()
        if yaml is not None:
            try:
                loaded = yaml.safe_load(raw_yaml)
            except Exception as exc:
                return {}, body, f"invalid YAML frontmatter: {exc}"
            if loaded is None:
                return {}, body, None
            if not isinstance(loaded, dict):
                return {}, body, f"frontmatter must be a mapping, got {type(loaded).__name__}"
            return dict(loaded), body, None
        parsed, err = _fallback_parse_frontmatter(raw_yaml)
        return parsed, body, err
    return {}, cleaned.strip(), None


def _yaml_scalar(value: str) -> str:
    if not value:
        return '""'
    if any(ch in value for ch in (":", "#", '"', "'", "\n", "[", "]", "{", "}")) or (
        value != value.strip()
    ):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        return f'"{escaped}"'
    return value


def format_frontmatter(meta: Mapping[str, Any], body: str) -> str:
    """Serialize simple YAML frontmatter deterministically without hard PyYAML reliance."""
    lines = ["---"]
    for key, val in meta.items():
        if isinstance(val, list):
            items = ", ".join(_yaml_scalar(str(item)) for item in val)
            lines.append(f"{key}: [{items}]")
        elif isinstance(val, bool):
            lines.append(f"{key}: {'true' if val else 'false'}")
        else:
            lines.append(f"{key}: {_yaml_scalar(str(val))}")
    lines.append("---")
    clean_body = body.strip()
    if clean_body:
        return "\n".join(lines) + "\n\n" + clean_body + "\n"
    return "\n".join(lines) + "\n"


def _is_archived_text(text: str) -> bool:
    meta, _, err = split_frontmatter(text)
    if err is not None:
        return False
    return str(meta.get("status", "")).strip().lower() == "archived"


def _slugify(value: str, *, fallback: str = "item") -> str:
    cleaned = _SLUG_CLEAN_RE.sub("-", (value or "").lower()).strip("-")
    return cleaned[:32] or fallback


def _extract_title_and_description(rel_path: str, text: str) -> tuple[str, str, str, str]:
    meta, body, err = split_frontmatter(text)
    if err is not None:
        meta = {}
    stem = Path(rel_path).stem
    title = str(meta.get("title") or "").strip()
    description = str(meta.get("description") or meta.get("summary") or "").strip()
    updated = str(meta.get("updated") or "").strip()
    update_rules = str(meta.get("update_rules") or "").strip()

    first_heading = ""
    first_line = ""
    for line in body.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("#") and not first_heading:
            first_heading = s.lstrip("#").strip()
            continue
        if s.startswith("<!--"):
            continue
        if not first_line:
            first_line = s.lstrip(">").strip()
            break

    final_title = title or first_heading or stem
    final_desc = description or first_line or final_title
    return final_title, final_desc[:180], updated, update_rules


class LearningStore:
    """Local-directory 3-layer learning store with atomic audit snapshots and recovery."""

    def __init__(
        self,
        root_dir: str | os.PathLike[str] | Callable[[], Path | str],
        *,
        seed_dir: str | os.PathLike[str] | None = None,
        inline_seeds: Mapping[str, str] | None = None,
        owner_agent: str = "agent",
        playbook_file: str = "playbook.md",
        mistakes_file: str = "mistakes.md",
        topics_dir: str = "topics",
        scopes_dir: str = "scopes",
        audit_dir: str = "audit",
        changelog_file: str = "CHANGELOG.md",
        playbook_max_chars: int = 12_000,
        mistakes_max_chars: int = 4_000,
        scope_max_chars: int = 3_000,
        timezone: str = "Asia/Shanghai",
        on_before_read: Callable[[str], None] | None = None,
        on_after_write: Callable[[str], None] | None = None,
    ) -> None:
        self._root_spec = root_dir
        self.seed_dir = Path(seed_dir).expanduser() if seed_dir is not None else None
        self.inline_seeds: dict[str, str] = dict(inline_seeds or {})
        self.owner_agent = owner_agent.strip() or "agent"
        self.playbook_file = playbook_file.strip("/")
        self.mistakes_file = mistakes_file.strip("/")
        self.topics_dir = topics_dir.strip("/")
        self.scopes_dir = scopes_dir.strip("/")
        self.audit_dir = audit_dir.strip("/")
        self.changelog_file = changelog_file.strip("/")
        self.playbook_max_chars = playbook_max_chars
        self.mistakes_max_chars = mistakes_max_chars
        self.scope_max_chars = scope_max_chars
        self.tz = _resolve_tz(timezone)
        self.on_before_read = on_before_read
        self.on_after_write = on_after_write
        self._lock = threading.RLock()
        self._seeded_roots: set[Path] = set()

    @property
    def root_dir(self) -> Path:
        raw = self._root_spec() if callable(self._root_spec) else self._root_spec
        return Path(raw).expanduser().resolve()

    # ------------------------------------------------------------------
    # Path normalization & layer classification
    # ------------------------------------------------------------------

    def normalize_rel_path(self, rel_path: str) -> str:
        raw = (rel_path or "").strip().replace("\\", "/")
        while raw.startswith("./"):
            raw = raw[2:]
        clean = raw.lstrip("/")
        if not clean or clean == ".":
            raise ValueError("Path cannot be empty")
        if "\x00" in clean:
            raise ValueError("Path contains null byte")
        parts = [p for p in clean.split("/") if p]
        if any(p in {".", ".."} for p in parts):
            raise ValueError(f"Path {rel_path!r} escapes learning directory")
        normalized = "/".join(parts)
        if is_credential_path(normalized):
            raise ValueError(f"Access denied: {rel_path!r} looks like a credential file")
        root = self.root_dir
        resolved = (root / normalized).resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            raise ValueError(f"Path {rel_path!r} escapes learning directory") from None
        return normalized

    def is_internal_path(self, rel_path: str) -> bool:
        try:
            norm = self.normalize_rel_path(rel_path)
        except ValueError:
            return True
        parts = norm.split("/")
        if any(part.startswith(".") for part in parts):
            return True
        top = parts[0]
        return top in {self.scopes_dir, self.audit_dir}

    def is_readme_path(self, rel_path: str) -> bool:
        return Path(rel_path).name.lower() == "readme.md"

    def classify_layer(self, rel_path: str) -> LearningLayer:
        if rel_path == self.playbook_file:
            return "playbook"
        if rel_path == self.mistakes_file:
            return "mistakes"
        if rel_path.startswith(f"{self.topics_dir}/"):
            return "topic"
        if rel_path.startswith(f"{self.scopes_dir}/users/"):
            return "user_scope"
        if rel_path.startswith(f"{self.scopes_dir}/groups/"):
            return "group_scope"
        return "system"

    def scope_rel_path(self, scope: Literal["user", "group"], scope_key: str) -> str:
        clean_key = (scope_key or "").strip()
        if not clean_key:
            raise ValueError("scope_key cannot be empty")
        sub = "users" if scope == "user" else "groups"
        slug = _slugify(clean_key, fallback=scope)
        digest = hashlib.sha256(clean_key.encode("utf-8")).hexdigest()[:12]
        return f"{self.scopes_dir}/{sub}/{slug}-{digest}.md"

    # ------------------------------------------------------------------
    # Seeding & self-describing READMEs
    # ------------------------------------------------------------------

    def _required_readmes(self) -> dict[str, str]:
        fmt = {
            "owner_agent": self.owner_agent,
            "playbook_file": self.playbook_file,
            "mistakes_file": self.mistakes_file,
            "topics_dir": self.topics_dir,
            "scopes_dir": self.scopes_dir,
            "audit_dir": self.audit_dir,
            "changelog_file": self.changelog_file,
        }
        return {
            "README.md": _ROOT_README_TEMPLATE.format(**fmt),
            f"{self.topics_dir}/README.md": _TOPICS_README_TEMPLATE.format(**fmt),
            f"{self.scopes_dir}/README.md": _SCOPES_README_TEMPLATE.format(**fmt),
            f"{self.audit_dir}/README.md": _AUDIT_README_TEMPLATE.format(**fmt),
        }

    def _seed_contents(self) -> dict[str, str]:
        seeds: dict[str, str] = {}
        if self.seed_dir is not None and self.seed_dir.is_dir():
            for src in sorted(self.seed_dir.rglob("*")):
                if not src.is_file() or src.suffix.lower() not in {".md", ".markdown"}:
                    continue
                rel = src.relative_to(self.seed_dir).as_posix()
                seeds[self.normalize_rel_path(rel)] = src.read_text(encoding="utf-8")
        for rel, content in self.inline_seeds.items():
            seeds[self.normalize_rel_path(rel)] = content
        seeds.setdefault(self.playbook_file, _DEFAULT_PLAYBOOK)
        seeds.setdefault(self.mistakes_file, _DEFAULT_MISTAKES)
        return seeds

    def ensure_seeded(self) -> list[str]:
        """Create directory skeleton, self-describing READMEs, and missing seeds."""
        with self._lock:
            root = self.root_dir
            if root in self._seeded_roots and (root / "README.md").is_file():
                return []

            for folder in (
                root,
                root / self.topics_dir,
                root / self.scopes_dir / "users",
                root / self.scopes_dir / "groups",
                root / self.audit_dir / "changes",
                root / self.audit_dir / "cursors",
            ):
                folder.mkdir(parents=True, exist_ok=True)

            created: list[str] = []
            for rel, content in self._required_readmes().items():
                target = root / rel
                if not target.is_file():
                    self._notify_before_read(rel)
                if not target.is_file():
                    self._write_raw(rel, content)
                    created.append(rel)

            if not (root / self.changelog_file).is_file():
                self._notify_before_read(self.changelog_file)
            if not (root / self.changelog_file).is_file():
                self._write_raw(self.changelog_file, _CHANGELOG_HEADER)
                created.append(self.changelog_file)

            seed_actor = LearningActor(actor_id=f"system:{self.owner_agent}", actor_name="seed")
            for rel, content in self._seed_contents().items():
                target = root / rel
                if not target.is_file():
                    self._notify_before_read(rel)
                if not target.is_file():
                    self._commit_write(
                        rel_path=rel,
                        new_content=content,
                        action="seed",
                        reason="Initial seed file",
                        actor=seed_actor,
                        update_changelog=False,
                    )
                    created.append(rel)

            self._seeded_roots.add(root)
            return created

    def _notify_before_read(self, rel_path: str) -> None:
        if self.on_before_read is not None:
            try:
                self.on_before_read(rel_path)
            except Exception:
                logger.warning("on_before_read hook failed for %s", rel_path, exc_info=True)

    def _write_raw(self, rel_path: str, content: str) -> None:
        target = self.root_dir / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        if self.on_after_write is not None:
            try:
                self.on_after_write(rel_path)
            except Exception:
                logger.warning("on_after_write hook failed for %s", rel_path, exc_info=True)

    # ------------------------------------------------------------------
    # Validation & safe read with snapshot fallback
    # ------------------------------------------------------------------

    def validate_markdown(self, rel_path: str, text: str) -> str | None:
        """Return an error string if ``text`` is structurally corrupted, else ``None``."""
        if "\x00" in text:
            return "contains null bytes"
        stripped = text.strip()
        if (rel_path == self.playbook_file or self.is_readme_path(rel_path)) and not stripped:
            return "core file is empty"
        _, _, err = split_frontmatter(text)
        if err is not None:
            return err
        return None

    def _latest_valid_snapshot(self, rel_path: str) -> LearningChangeRecord | None:
        for record in self._load_all_changes(newest_first=True):
            if record.target != rel_path or record.after_content is None:
                continue
            if self.validate_markdown(rel_path, record.after_content) is None:
                return record
        return None

    def _fallback_content(self, rel_path: str) -> str | None:
        snap = self._latest_valid_snapshot(rel_path)
        if snap is not None and snap.after_content is not None:
            return snap.after_content
        readmes = self._required_readmes()
        if rel_path in readmes:
            return readmes[rel_path]
        seeds = self._seed_contents()
        if rel_path in seeds:
            return seeds[rel_path]
        if rel_path == self.changelog_file:
            return _CHANGELOG_HEADER
        return None

    def read_text(self, rel_path: str, *, include_archived: bool = False) -> str | None:
        """Read a Markdown document safely, falling back to last valid snapshot if corrupted."""
        self.ensure_seeded()
        norm = self.normalize_rel_path(rel_path)
        self._notify_before_read(norm)
        target = self.root_dir / norm
        if not target.is_file():
            return None
        try:
            raw_bytes = target.read_bytes()
            text = raw_bytes.decode("utf-8", errors="strict")
        except (OSError, UnicodeDecodeError) as exc:
            logger.warning("Corrupted learning file %s (%s); using fallback", norm, exc)
            fallback = self._fallback_content(norm)
            if fallback is None:
                return None
            if not include_archived and _is_archived_text(fallback):
                return None
            return fallback

        err = self.validate_markdown(norm, text)
        if err is not None:
            logger.warning("Invalid learning file %s (%s); using fallback", norm, err)
            fallback = self._fallback_content(norm)
            if fallback is None:
                return None
            if not include_archived and _is_archived_text(fallback):
                return None
            return fallback

        if not include_archived and _is_archived_text(text):
            return None
        return text

    # ------------------------------------------------------------------
    # Audit log & changelog helpers
    # ------------------------------------------------------------------

    def _load_all_changes(self, *, newest_first: bool = True) -> list[LearningChangeRecord]:
        changes_dir = self.root_dir / self.audit_dir / "changes"
        if not changes_dir.is_dir():
            return []
        records: list[LearningChangeRecord] = []
        paths = sorted(changes_dir.glob("*.json"), reverse=newest_first)
        for path in paths:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                records.append(LearningChangeRecord.model_validate(data))
            except Exception as exc:
                logger.warning("Ignoring unreadable audit file %s: %s", path.name, exc)
        return records

    def list_changes(
        self,
        *,
        limit: int = 20,
        layer: LearningLayer | None = None,
        actor: LearningActor | None = None,
        include_system: bool = False,
    ) -> list[LearningChangeRecord]:
        """List audit records newest-first, filtering private scopes to the caller."""
        self.ensure_seeded()
        self._notify_before_read(self.audit_dir)
        user_target = (
            self.scope_rel_path("user", actor.actor_id)
            if actor is not None and actor.actor_id not in {"", "unknown"}
            else None
        )
        group_target = (
            self.scope_rel_path("group", actor.chat_id)
            if actor is not None and actor.chat_id
            else None
        )

        out: list[LearningChangeRecord] = []
        for rec in self._load_all_changes(newest_first=True):
            if not include_system and rec.action == "seed":
                continue
            if layer is not None and rec.layer != layer:
                continue
            if actor is not None:
                if rec.layer == "user_scope" and rec.target != user_target:
                    continue
                if rec.layer == "group_scope" and rec.target != group_target:
                    continue
            out.append(rec)
            if limit > 0 and len(out) >= limit:
                break
        return out

    def get_change(self, change_id: str) -> LearningChangeRecord | None:
        clean = change_id.strip()
        if not clean:
            return None
        self._notify_before_read(self.audit_dir)
        for rec in self._load_all_changes(newest_first=True):
            if rec.change_id == clean:
                return rec
        return None

    def _append_changelog(self, record: LearningChangeRecord) -> None:
        if record.layer in {"user_scope", "group_scope"} or record.action == "seed":
            return
        local_ts = (
            datetime.fromisoformat(record.timestamp).astimezone(self.tz).strftime("%Y-%m-%d %H:%M")
        )
        who = record.actor_name or record.actor_id or "unknown"
        if record.platform:
            who = f"{who} via {record.platform}"
        entry = (
            f"- **{local_ts}** `{record.change_id}` [{record.action}] "
            f"`{record.target}` — {record.reason} *({who})*"
        )
        existing = self.read_text(self.changelog_file, include_archived=True)
        if existing is None or _CHANGELOG_MARKER not in existing:
            base = _CHANGELOG_HEADER
        else:
            base = existing
        before_marker, after_marker = base.split(_CHANGELOG_MARKER, 1)
        rest = after_marker.lstrip("\n")
        updated = f"{before_marker}{_CHANGELOG_MARKER}\n{entry}\n{rest}"
        self._write_raw(self.changelog_file, updated)

    def _commit_write(
        self,
        *,
        rel_path: str,
        new_content: str,
        action: LearningAction,
        reason: str,
        actor: LearningActor | None = None,
        reverted_change_id: str | None = None,
        update_changelog: bool = True,
        before_content_override: str | None = None,
    ) -> LearningChangeRecord:
        with self._lock:
            target = self.root_dir / rel_path
            before_content: str | None = before_content_override
            if before_content is None and target.is_file():
                try:
                    before_content = target.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    before_content = None

            self._write_raw(rel_path, new_content)
            now = _utc_now()
            change_id = f"chg-{uuid.uuid4().hex[:8]}"
            diff_lines = list(
                difflib.unified_diff(
                    (before_content or "").splitlines(),
                    new_content.splitlines(),
                    fromfile=f"a/{rel_path}",
                    tofile=f"b/{rel_path}",
                    lineterm="",
                )
            )
            resolved_actor = actor or LearningActor()
            record = LearningChangeRecord(
                change_id=change_id,
                timestamp=now.isoformat(),
                layer=self.classify_layer(rel_path),
                target=rel_path,
                action=action,
                reason=reason.strip() or action,
                actor_id=resolved_actor.actor_id,
                actor_name=resolved_actor.actor_name,
                platform=resolved_actor.platform,
                chat_id=resolved_actor.chat_id,
                session_id=resolved_actor.session_id,
                diff="\n".join(diff_lines),
                before_content=before_content,
                after_content=new_content,
                after_sha256=_sha256(new_content),
                reverted_change_id=reverted_change_id,
            )
            stamp = now.strftime("%Y%m%dT%H%M%S%fZ")
            audit_rel = f"{self.audit_dir}/changes/{stamp}-{change_id}.json"
            self._write_raw(audit_rel, record.model_dump_json(indent=2) + "\n")
            if update_changelog:
                self._append_changelog(record)
            return record

    def _compaction_hint(self, layer: LearningLayer, text: str) -> str:
        limit_map = {
            "playbook": self.playbook_max_chars,
            "mistakes": self.mistakes_max_chars,
            "user_scope": self.scope_max_chars,
            "group_scope": self.scope_max_chars,
        }
        limit = limit_map.get(layer)
        if limit is None or len(text) <= limit:
            return ""
        return (
            f"\n[COMPACTION HINT] '{layer}' is now {len(text)} chars (soft limit {limit}). "
            "Consider merging duplicate bullets or moving detailed reference material into a topic file."
        )

    # ------------------------------------------------------------------
    # Public write API (Layers 1, 2, 3 & Rollback)
    # ------------------------------------------------------------------

    def write_document(
        self,
        rel_path: str,
        content: str,
        *,
        mode: str = "write",
        reason: str = "direct document write",
        actor: LearningActor | None = None,
        action: LearningAction | None = None,
    ) -> tuple[LearningChangeRecord, str]:
        """Write or append a document in Layer 1 or Layer 2 with audit snapshot."""
        self.ensure_seeded()
        norm = self.normalize_rel_path(rel_path)
        if mode not in {"write", "append"}:
            raise ValueError(f"Unsupported write mode {mode!r}; expected 'write' or 'append'")
        if self.is_internal_path(norm):
            raise ValueError(f"Security error: cannot directly write internal path {norm!r}")

        with self._lock:
            self._notify_before_read(norm)
            existing = self.read_text(norm, include_archived=True)
            if mode == "append" and existing is not None and not _is_archived_text(existing):
                sep = "" if (not existing or existing.endswith("\n")) else "\n"
                new_content = existing + sep + content
                chosen_action: LearningAction = action or "append"
            else:
                new_content = content
                chosen_action = action or ("create" if existing is None else "write")

            err = self.validate_markdown(norm, new_content)
            if err is not None:
                raise ValueError(f"Invalid markdown content for {norm!r}: {err}")

            record = self._commit_write(
                rel_path=norm,
                new_content=new_content,
                action=chosen_action,
                reason=reason,
                actor=actor,
            )
            hint = self._compaction_hint(record.layer, new_content)
            return (
                record,
                f"Successfully wrote {len(content)} characters to '{norm}' (change: {record.change_id}).{hint}",
            )

    def patch_document(
        self,
        rel_path: str,
        search_block: str,
        replace_block: str,
        *,
        reason: str = "direct file patch",
        actor: LearningActor | None = None,
        action: LearningAction = "patch",
    ) -> tuple[LearningChangeRecord, str]:
        """Search-and-replace a unique block inside a Layer 1/Layer 2 document."""
        self.ensure_seeded()
        norm = self.normalize_rel_path(rel_path)
        if self.is_internal_path(norm):
            raise ValueError(f"Security error: cannot directly patch internal path {norm!r}")

        with self._lock:
            content = self.read_text(norm, include_archived=False)
            if content is None:
                raise FileNotFoundError(f"File '{norm}' not found.")
            count = content.count(search_block)
            if count == 0:
                raise ValueError(
                    "`search_block` was not found in file. Ensure exact matching of characters, whitespace, and indentation."
                )
            if count > 1:
                raise ValueError(
                    f"`search_block` matched {count} times. Provide more surrounding context to ensure a unique match."
                )
            updated = content.replace(search_block, replace_block, 1)
            err = self.validate_markdown(norm, updated)
            if err is not None:
                raise ValueError(f"Patched markdown for {norm!r} is invalid: {err}")

            record = self._commit_write(
                rel_path=norm,
                new_content=updated,
                action=action,
                reason=reason,
                actor=actor,
            )
            hint = self._compaction_hint(record.layer, updated)
            return record, f"Successfully patched '{norm}' (change: {record.change_id}).{hint}"

    def record_mistake(
        self,
        rule: str,
        *,
        reason: str = "correction from user",
        actor: LearningActor | None = None,
        action: LearningAction = "append",
    ) -> tuple[LearningChangeRecord | None, str]:
        """Prepend a newest-first mistake entry into ``mistakes.md``, skipping duplicates."""
        self.ensure_seeded()
        clean_rule = " ".join(
            line.strip() for line in (rule or "").strip().splitlines() if line.strip()
        )
        if not clean_rule:
            raise ValueError("rule cannot be empty")
        clean_rule = _MISTAKE_PREFIX_RE.sub("", clean_rule).strip()

        with self._lock:
            current = self.read_text(self.mistakes_file, include_archived=False)
            if current is None:
                current = _DEFAULT_MISTAKES

            lines = current.splitlines()
            for line in lines:
                stripped = line.strip()
                if stripped.startswith("- ") or stripped.startswith("* "):
                    existing_body = _MISTAKE_PREFIX_RE.sub("", stripped).strip()
                    if existing_body.casefold() == clean_rule.casefold():
                        return None, f"Mistake already recorded in '{self.mistakes_file}'."

            day_str = datetime.now(self.tz).strftime("%Y-%m-%d")
            bullet = f"- [{day_str}] {clean_rule}"
            new_content = _insert_newest_first_bullet(lines, bullet)
            record = self._commit_write(
                rel_path=self.mistakes_file,
                new_content=new_content,
                action=action,
                reason=reason,
                actor=actor,
            )
            hint = self._compaction_hint("mistakes", new_content)
            return (
                record,
                f"Recorded mistake in '{self.mistakes_file}' (change: {record.change_id}).{hint}",
            )

    def read_preference(self, scope: Literal["user", "group"], scope_key: str) -> str:
        """Read the active preference body for a user or group scope."""
        self.ensure_seeded()
        rel = self.scope_rel_path(scope, scope_key)
        text = self.read_text(rel, include_archived=False)
        if not text:
            return ""
        _, body, err = split_frontmatter(text)
        if err is not None:
            return ""
        return body.strip()

    def update_preference(
        self,
        scope: Literal["user", "group"],
        scope_key: str,
        content: str,
        *,
        label: str = "",
        mode: str = "replace",
        reason: str = "update preference",
        actor: LearningActor | None = None,
        action: LearningAction | None = None,
    ) -> tuple[LearningChangeRecord, str]:
        """Create or update a Layer 3 scoped user/group preference file."""
        self.ensure_seeded()
        if mode not in {"replace", "append"}:
            raise ValueError(f"Unsupported mode {mode!r}; expected 'replace' or 'append'")
        rel = self.scope_rel_path(scope, scope_key)
        day_str = datetime.now(self.tz).strftime("%Y-%m-%d")

        with self._lock:
            existing_raw = self.read_text(rel, include_archived=True)
            existing_meta: dict[str, Any] = {}
            existing_body = ""
            if existing_raw and not _is_archived_text(existing_raw):
                existing_meta, existing_body, _ = split_frontmatter(existing_raw)

            clean_new = content.strip()
            if mode == "append":
                if not clean_new:
                    raise ValueError("content cannot be empty when mode='append'")
                body = (
                    f"{existing_body.strip()}\n{clean_new}".strip()
                    if existing_body.strip()
                    else clean_new
                )
            else:
                body = clean_new

            if not body:
                tombstone = format_frontmatter(
                    {
                        "type": "scope",
                        "scope": scope,
                        "label": label or str(existing_meta.get("label") or scope_key),
                        "updated": day_str,
                        "status": "archived",
                        "archived_reason": reason,
                    },
                    "",
                )
                record = self._commit_write(
                    rel_path=rel,
                    new_content=tombstone,
                    action=action or "archive",
                    reason=reason,
                    actor=actor,
                )
                return record, f"Cleared {scope} preference (change: {record.change_id})."

            meta = {
                "type": "scope",
                "scope": scope,
                "label": label or str(existing_meta.get("label") or scope_key),
                "updated": day_str,
                "status": "active",
            }
            formatted = format_frontmatter(meta, body)
            chosen_action: LearningAction = action or (
                "create" if not existing_raw or _is_archived_text(existing_raw) else "write"
            )
            record = self._commit_write(
                rel_path=rel,
                new_content=formatted,
                action=chosen_action,
                reason=reason,
                actor=actor,
            )
            layer: LearningLayer = "user_scope" if scope == "user" else "group_scope"
            hint = self._compaction_hint(layer, body)
            return record, f"Updated {scope} preference (change: {record.change_id}).{hint}"

    def revert(
        self,
        change_id: str,
        *,
        reason: str = "reverted by user",
        actor: LearningActor | None = None,
    ) -> LearningChangeRecord:
        """Roll back a specific change ID, archiving newly created files with a tombstone."""
        self.ensure_seeded()
        with self._lock:
            target_rec = self.get_change(change_id)
            if target_rec is None:
                raise KeyError(f"Change '{change_id}' not found.")

            if target_rec.before_content is None:
                day_str = datetime.now(self.tz).strftime("%Y-%m-%d")
                restored = format_frontmatter(
                    {
                        "status": "archived",
                        "updated": day_str,
                        "archived_reason": f"Reverted {change_id}: {reason}",
                    },
                    "",
                )
            else:
                restored = target_rec.before_content

            return self._commit_write(
                rel_path=target_rec.target,
                new_content=restored,
                action="revert",
                reason=f"Revert {change_id}: {reason}",
                actor=actor,
                reverted_change_id=change_id,
            )

    # ------------------------------------------------------------------
    # Self-healing doctor
    # ------------------------------------------------------------------

    def doctor(
        self,
        *,
        repair: bool = False,
        restore_untracked_drift: bool = False,
        actor: LearningActor | None = None,
    ) -> DoctorReport:
        """Inspect and optionally repair missing, corrupted, or externally drifted files."""
        with self._lock:
            root = self.root_dir
            issues: list[DoctorIssue] = []
            checked = 0
            heal_actor = actor or LearningActor(
                actor_id=f"system:{self.owner_agent}", actor_name="doctor"
            )

            # 1. Check required directories and READMEs
            if repair:
                for folder in (
                    root,
                    root / self.topics_dir,
                    root / self.scopes_dir / "users",
                    root / self.scopes_dir / "groups",
                    root / self.audit_dir / "changes",
                    root / self.audit_dir / "cursors",
                ):
                    folder.mkdir(parents=True, exist_ok=True)

            for rel, default_readme in self._required_readmes().items():
                checked += 1
                self._notify_before_read(rel)
                path = root / rel
                if not path.is_file():
                    issue = DoctorIssue(
                        path=rel,
                        kind="missing",
                        detail="Required README.md is missing",
                    )
                    if repair:
                        self._write_raw(rel, default_readme)
                        issue.repaired = True
                        issue.action_taken = "Recreated README.md"
                    issues.append(issue)
                else:
                    try:
                        text = path.read_bytes().decode("utf-8", errors="strict")
                        err = self.validate_markdown(rel, text)
                    except (OSError, UnicodeDecodeError) as exc:
                        err = str(exc)
                    if err is not None:
                        issue = DoctorIssue(path=rel, kind="corrupted", detail=err)
                        if repair:
                            self._write_raw(rel, default_readme)
                            issue.repaired = True
                            issue.action_taken = "Restored default README.md"
                        issues.append(issue)

            # 2. Check core seed files (playbook.md, mistakes.md, seed files)
            seeds = self._seed_contents()
            for rel, seed_body in seeds.items():
                checked += 1
                self._notify_before_read(rel)
                path = root / rel
                if not path.is_file():
                    issue = DoctorIssue(path=rel, kind="missing", detail="Seed file is missing")
                    if repair:
                        restored = self._fallback_content(rel) or seed_body
                        self._commit_write(
                            rel_path=rel,
                            new_content=restored,
                            action="repair",
                            reason="Doctor restored missing seed file",
                            actor=heal_actor,
                        )
                        issue.repaired = True
                        issue.action_taken = "Restored from snapshot or seed"
                    issues.append(issue)

            # 3. Validate latest tracked files against audit snapshots
            latest_by_target: dict[str, LearningChangeRecord] = {}
            for rec in self._load_all_changes(newest_first=False):
                latest_by_target[rec.target] = rec

            seen_md: set[str] = set()
            for rel, rec in sorted(latest_by_target.items()):
                if rel in self._required_readmes() or rel == self.changelog_file:
                    continue
                seen_md.add(rel)
                checked += 1
                self._notify_before_read(rel)
                path = root / rel
                if not path.is_file():
                    if rel not in seeds:
                        issue = DoctorIssue(
                            path=rel,
                            kind="missing",
                            detail="Tracked learning file missing on disk",
                        )
                        if repair and rec.after_content is not None:
                            self._commit_write(
                                rel_path=rel,
                                new_content=rec.after_content,
                                action="repair",
                                reason=f"Doctor restored missing file from {rec.change_id}",
                                actor=heal_actor,
                            )
                            issue.repaired = True
                            issue.action_taken = f"Restored from {rec.change_id}"
                        issues.append(issue)
                    continue

                err, disk_text = self._read_and_validate_disk(rel)
                if err is not None:
                    issue = DoctorIssue(path=rel, kind="corrupted", detail=err)
                    if repair:
                        fallback = self._fallback_content(rel)
                        if fallback is not None:
                            self._commit_write(
                                rel_path=rel,
                                new_content=fallback,
                                action="repair",
                                reason=f"Doctor repaired corrupted file ({err})",
                                actor=heal_actor,
                            )
                            issue.repaired = True
                            issue.action_taken = "Restored from last valid snapshot"
                    issues.append(issue)
                    continue

                assert disk_text is not None
                actual_sha = _sha256(disk_text)
                if rec.after_sha256 and actual_sha != rec.after_sha256:
                    issue = DoctorIssue(
                        path=rel,
                        kind="external_drift",
                        detail="File modified outside LearningStore audit chain",
                    )
                    if repair:
                        if restore_untracked_drift and rec.after_content is not None:
                            self._commit_write(
                                rel_path=rel,
                                new_content=rec.after_content,
                                action="repair",
                                reason=f"Doctor reverted external drift to {rec.change_id}",
                                actor=heal_actor,
                            )
                            issue.repaired = True
                            issue.action_taken = f"Reverted drift to {rec.change_id}"
                        else:
                            ext_actor = LearningActor(
                                actor_id="external:vault", actor_name="external edit"
                            )
                            self._commit_write(
                                rel_path=rel,
                                new_content=disk_text,
                                action="external_sync",
                                reason="Captured external vault edit into audit chain",
                                actor=ext_actor,
                                before_content_override=rec.after_content,
                            )
                            issue.repaired = True
                            issue.action_taken = "Recorded external edit in audit chain"
                    issues.append(issue)

            # 4. Scan remaining untracked .md files under root/topics/scopes for corruption
            for md_path in sorted(root.rglob("*.md")):
                rel = md_path.relative_to(root).as_posix()
                if rel in seen_md or rel in self._required_readmes():
                    continue
                if rel.startswith(f"{self.audit_dir}/"):
                    continue
                checked += 1
                err, _ = self._read_and_validate_disk(rel)
                if err is not None:
                    issue = DoctorIssue(path=rel, kind="corrupted", detail=err)
                    if repair:
                        fallback = self._fallback_content(rel)
                        if fallback is not None:
                            self._commit_write(
                                rel_path=rel,
                                new_content=fallback,
                                action="repair",
                                reason=f"Doctor repaired corrupted untracked file ({err})",
                                actor=heal_actor,
                            )
                            issue.repaired = True
                            issue.action_taken = "Restored from snapshot or seed"
                    issues.append(issue)

            # 5. Validate audit changes & cursor JSON files
            for folder_name in ("changes", "cursors"):
                folder = root / self.audit_dir / folder_name
                if not folder.is_dir():
                    continue
                for json_path in sorted(folder.glob("*.json")):
                    checked += 1
                    rel = json_path.relative_to(root).as_posix()
                    try:
                        data = json.loads(json_path.read_text(encoding="utf-8"))
                        if folder_name == "changes":
                            LearningChangeRecord.model_validate(data)
                        elif not isinstance(data, dict):
                            raise ValueError("cursor must be a JSON object")
                    except Exception as exc:
                        issue = DoctorIssue(path=rel, kind="corrupted", detail=str(exc))
                        if repair and folder_name == "cursors":
                            json_path.unlink(missing_ok=True)
                            issue.repaired = True
                            issue.action_taken = "Removed corrupted cursor file"
                        issues.append(issue)

            ok = all(item.repaired for item in issues)
            return DoctorReport(ok=ok, checked_files=checked, issues=issues)

    def _read_and_validate_disk(self, rel_path: str) -> tuple[str | None, str | None]:
        target = self.root_dir / rel_path
        try:
            text = target.read_bytes().decode("utf-8", errors="strict")
        except (OSError, UnicodeDecodeError) as exc:
            return str(exc), None
        err = self.validate_markdown(rel_path, text)
        return err, text

    # ------------------------------------------------------------------
    # Catalog & live prompt assembly
    # ------------------------------------------------------------------

    def list_topics(self) -> list[TopicCatalogEntry]:
        """Scan ``<topics_dir>/*.md`` and build live catalog metadata."""
        self.ensure_seeded()
        self._notify_before_read(self.topics_dir)
        topics_path = self.root_dir / self.topics_dir
        if not topics_path.is_dir():
            return []

        entries: list[TopicCatalogEntry] = []
        for path in sorted(topics_path.glob("*.md")):
            if path.name.lower() == "readme.md":
                continue
            rel = path.relative_to(self.root_dir).as_posix()
            text = self.read_text(rel, include_archived=False)
            if text is None:
                continue
            title, desc, updated, update_rules = _extract_title_and_description(rel, text)
            entries.append(
                TopicCatalogEntry(
                    slug=path.stem,
                    path=rel,
                    title=title,
                    description=desc,
                    updated=updated,
                    update_rules=update_rules,
                )
            )
        return entries

    def render_context(
        self,
        run_context: Any = None,
        *,
        include_scopes: bool = True,
        heading: str = "## Living Playbook & Learned Context",
    ) -> str:
        """Render the live 3-layer learning context for the current turn."""
        self.ensure_seeded()
        actor = resolve_learning_actor(run_context)

        sections: list[str] = [
            heading,
            (
                "Instruction Precedence: "
                "(1) Explicit requirement in the current user turn > "
                "(2) Active User/Group preferences > "
                "(3) Standing Playbook & Common Mistakes."
            ),
        ]

        playbook_text = (self.read_text(self.playbook_file) or "").strip()
        if playbook_text:
            sections.append(playbook_text)

        mistakes_text = (self.read_text(self.mistakes_file) or "").strip()
        if _has_list_items(mistakes_text):
            sections.append(mistakes_text)

        topics = self.list_topics()
        if topics:
            catalog_lines = [
                f"### Topic & Glossary Catalog (`{self.topics_dir}/`, read on demand with `read` or `grep`)"
            ]
            for entry in topics:
                suffix = f" (updated: {entry.updated})" if entry.updated else ""
                catalog_lines.append(
                    f"- `{entry.path}` (`{entry.slug}`): {entry.description}{suffix}"
                )
            sections.append("\n".join(catalog_lines))

        if include_scopes:
            if actor.chat_id and actor.is_direct_message is not True:
                group_pref = self.read_preference("group", actor.chat_id)
                if group_pref:
                    sections.append(
                        f"### Active Group Preference (`{actor.chat_id}`)\n\n{group_pref}"
                    )
            if actor.actor_id and actor.actor_id != "unknown":
                user_pref = self.read_preference("user", actor.actor_id)
                if user_pref:
                    label = actor.actor_name or actor.actor_id
                    sections.append(f"### Active User Preference ({label})\n\n{user_pref}")

        return "\n\n".join(part for part in sections if part.strip())

    def compose_instructions(
        self,
        *parts: str | Callable[..., str],
        include_scopes: bool = True,
        heading: str = "## Living Playbook & Learned Context",
    ) -> Callable[..., str]:
        """Build a dynamic ``Agent(instructions=...)`` callable evaluated fresh every turn."""

        def _dynamic_instructions(run_context: Any = None) -> str:
            rendered: list[str] = []
            for part in parts:
                val = part(run_context) if callable(part) else part
                if val and val.strip():
                    rendered.append(val.strip())
            learning_ctx = self.render_context(
                run_context,
                include_scopes=include_scopes,
                heading=heading,
            )
            if learning_ctx.strip():
                rendered.append(learning_ctx.strip())
            return "\n\n".join(rendered)

        return _dynamic_instructions


def _has_list_items(markdown_text: str) -> bool:
    for line in markdown_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("- ") or stripped.startswith("* "):
            return True
    return False


def _insert_newest_first_bullet(lines: list[str], bullet: str) -> str:
    """Insert ``bullet`` at the top of the dynamic list without disturbing header prose."""
    for idx, line in enumerate(lines):
        if line.strip() == _MISTAKES_MARKER:
            insert_at = idx + 1
            while insert_at < len(lines) and not lines[insert_at].strip():
                insert_at += 1
            updated = [*lines[:insert_at], bullet, *lines[insert_at:]]
            return "\n".join(updated).rstrip() + "\n"

    for idx, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("- ") or stripped.startswith("* "):
            updated = [*lines[:idx], bullet, *lines[idx:]]
            return "\n".join(updated).rstrip() + "\n"

    base = "\n".join(lines).rstrip()
    if not base:
        return bullet + "\n"
    return f"{base}\n\n{bullet}\n"


__all__ = [
    "LearningStore",
    "format_frontmatter",
    "split_frontmatter",
]
