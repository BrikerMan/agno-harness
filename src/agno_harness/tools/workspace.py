# ==============================================================================
# File:         workspace.py
# Author:       Eliyar Eziz(eliyar917@gmail.com)
# Description:  Safe workspace access and manipulation toolkit for agno-harness.
#               Provides isolated read/glob/grep tools, with optional write/patch.
#
# Usage:
#     from agno_harness.tools import WorkspaceToolkit
#     ws = WorkspaceToolkit(allow_write=False)
#
# Copyright (c) 2026. All rights reserved.
# ==============================================================================

from __future__ import annotations

import difflib
import inspect
import logging
import mimetypes
import os
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from agno.run import RunContext
from agno.tools.toolkit import Toolkit

from ..runtime.modules.streamui import resolve_artifact_dir
from ..runtime.scope import RunScope, current_scope

logger = logging.getLogger(__name__)

_DEFAULT_HIDDEN_NAMES: frozenset[str] = frozenset(
    {".manifest.json", "__pycache__", ".DS_Store", ".git"}
)


@dataclass(frozen=True)
class WorkspaceMount:
    """A named virtual folder mounted into ``WorkspaceToolkit`` with independent permissions."""

    prefix: str
    root: str | os.PathLike[str] | Callable[..., Path | str]
    allow_write: bool = False
    description: str = ""
    aliases: tuple[str, ...] = ()
    file_resolver: Callable[[Path, str], Path | None] | None = None
    hidden_names: frozenset[str] | None = None
    is_path_blocked: Callable[[str], bool] | None = None
    write_handler: Callable[[str, str, str, str, RunContext | None], str] | None = None
    patch_handler: Callable[[str, str, str, str, RunContext | None], str] | None = None
    on_write: Callable[[str, Path], None] | None = None


def _call_root_provider(
    root: str | os.PathLike[str] | Callable[..., Path | str],
    scope: RunScope | None,
) -> Path:
    if callable(root):
        sig = inspect.signature(root)
        params = [
            p
            for p in sig.parameters.values()
            if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD, p.VAR_POSITIONAL)
        ]
        if not params:
            return Path(root()).expanduser().resolve()
    return resolve_artifact_dir(root, scope)


# Files an agent has no business reading: secrets and keys. Matched on the
# lower-cased file name, or on any directory component of the relative path.
_CREDENTIAL_FILE_PATTERNS: tuple[str, ...] = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "credentials",
    "credentials.json",
    "*.tfvars",
    "*.tfstate",
    ".netrc",
    ".pgpass",
    ".npmrc",
    ".pypirc",
)
_CREDENTIAL_DIR_NAMES: frozenset[str] = frozenset({".ssh", ".aws", ".gnupg", ".kube"})
# Committed templates carry no secrets, so ``.env.example`` stays readable.
_TEMPLATE_SUFFIXES: tuple[str, ...] = (".example", ".sample", ".template", ".dist")


def is_credential_path(rel_path: str) -> bool:
    """True when a workspace-relative path looks like a secret or key file."""
    parts = [p for p in rel_path.replace("\\", "/").split("/") if p and p != "."]
    if not parts:
        return False
    if any(part.lower() in _CREDENTIAL_DIR_NAMES for part in parts[:-1]):
        return True
    name = parts[-1].lower()
    if name in _CREDENTIAL_DIR_NAMES:
        return True
    if name.endswith(_TEMPLATE_SUFFIXES):
        return False
    return any(fnmatch(name, pattern) for pattern in _CREDENTIAL_FILE_PATTERNS)


class WorkspaceToolkit(Toolkit):
    """Agno Toolkit providing safe, isolated workspace inspection and manipulation.

    By default (``allow_write=False``), only read-only exploration tools are registered:
    - ``read(path, offset=1, limit=200)``: Read file slice or inspect directory.
    - ``glob(pattern="*", base="")``: Find paths matching glob pattern.
    - ``grep(pattern, path=".", is_regexp=False)``: Search file content.
    - ``list_files(pattern="**/*")``: List all files in the workspace.

    When ``allow_write=True``, writing and patching tools are also registered:
    - ``write_file(path, content, mode="write")``: Write or append to a file.
    - ``patch_file(path, search_block, replace_block)``: Exact Search & Replace patch.

    Credential files are refused by default: ``.env`` (but not ``.env.example``),
    ``*.pem``, ``*.key``, ``.ssh/``, ``.aws/``, ``credentials.json``, ``*.tfvars``
    and similar. They are also left out of listings and searches. Re-allow
    specific paths with ``allow_paths=["config/credentials.json"]`` (glob patterns
    relative to the workspace root), or turn the guard off with
    ``block_credentials=False``.
    """

    def __init__(
        self,
        workspace_dir: str | os.PathLike[str] | Callable[[RunScope], Path | str] | None = None,
        *,
        file_resolver: Callable[[Path, str], Path | None] | None = None,
        allow_write: bool = False,
        mounts: Sequence[WorkspaceMount] = (),
        default_mount: str | None = None,
        max_entries: int = 100,
        max_matches: int = 50,
        name: str = "workspace",
        instructions: str | None = None,
        add_instructions: bool = True,
        hidden_names: frozenset[str] | None = None,
        block_credentials: bool = True,
        allow_paths: Sequence[str] = (),
        **toolkit_kwargs: Any,
    ) -> None:
        self.block_credentials = block_credentials
        self.allow_paths = tuple(allow_paths)
        self.workspace_dir = workspace_dir
        self.file_resolver = file_resolver
        self.allow_write = allow_write
        self.mounts = tuple(mounts)
        self.default_mount = default_mount.strip("/") if default_mount else None
        self.max_entries = max_entries
        self.max_matches = max_matches
        self.hidden_names = hidden_names if hidden_names is not None else _DEFAULT_HIDDEN_NAMES

        has_any_write = self.allow_write or any(m.allow_write for m in self.mounts)
        instruction_text = instructions or self._render_default_instructions()
        super().__init__(
            name=name,
            instructions=instruction_text,
            add_instructions=add_instructions,
            **toolkit_kwargs,
        )

        self.register(self.read, name="read")
        self.register(self.glob, name="glob")
        self.register(self.grep, name="grep")
        self.register(self.list_files, name="list_files")
        self.register(self.list_artifacts, name="list_artifacts")
        self.register(self.diff_files, name="diff_files")

        if has_any_write:
            self.register(self.write_file, name="write_file")
            self.register(self.patch_file, name="patch_file")

    def _render_default_instructions(self) -> str:
        has_any_write = self.allow_write or any(m.allow_write for m in self.mounts)
        lines = [
            "You have access to safe workspace inspection tools (`read`, `glob`, `grep`, `list_files`, `diff_files`) "
            "to explore existing files, reports, and code artifacts in the current workspace.",
        ]
        if self.mounts:
            lines.append("Mounted folders:")
            for m in self.mounts:
                perm = "rw" if m.allow_write else "ro"
                desc = f" — {m.description}" if m.description else ""
                lines.append(f"  - [{perm}] `{m.prefix.strip('/')}/`{desc}")
        lines.extend(
            [
                "- Use `list_files` or `glob` to discover available files.",
                "- Use `read` with `offset` and `limit` to inspect specific lines of a file without loading entire large files.",
                "- Use `grep` to quickly search keywords or regex across files.",
                "- Use `diff_files` to inspect unified diffs between two files or snapshots.",
            ]
        )
        if has_any_write:
            lines.extend(
                [
                    "- Use `write_file` to write or append content to files (when allowed by folder permissions).",
                    "- Use `patch_file` for precise Search & Replace modifications without overwriting entire files.",
                ]
            )
        else:
            lines.append(
                "NOTE: File modification tools are disabled in this environment. To create long documents, reports, or decks, "
                "stream them using `<stream-ui>` blocks in your response."
            )
        return "\n".join(lines)

    def _get_scope(self, run_context: RunContext | None = None) -> RunScope | None:
        scope = current_scope()
        if scope is None and run_context is not None:
            session_state = getattr(run_context, "session_state", None) or {}
            user_id = session_state.get("user_id")
            thread_id = session_state.get("thread_id") or getattr(run_context, "session_id", None)
            from ..runtime.translator import make_run_scope

            scope = make_run_scope(
                thread_id=str(thread_id).strip() if thread_id else "default",
                user_id=str(user_id).strip() if user_id else None,
            )
        return scope

    def resolve_workspace_root(self, run_context: RunContext | None = None) -> Path:
        """Resolve the active workspace root directory against current RunScope or RunContext."""
        scope = self._get_scope(run_context)

        if self.workspace_dir is not None:
            return _call_root_provider(self.workspace_dir, scope)

        if scope is not None and scope.data.get("artifact_root_dir"):
            return resolve_artifact_dir(scope.data["artifact_root_dir"], scope)

        # Fallback using scope or run_context
        thread_id_val: str | None = None
        user_id_val: str | None = None
        if scope:
            thread_id_val = scope.thread_id
            user_id_val = scope.user_id

        if run_context:
            session_state = getattr(run_context, "session_state", None) or {}
            user_id_val = user_id_val or session_state.get("user_id")
            thread_id_val = (
                thread_id_val
                or session_state.get("thread_id")
                or getattr(run_context, "session_id", None)
            )

        template = "workspaces/{user_id}/{thread_id}"
        expanded = template.replace("{user_id}", user_id_val or "default").replace(
            "{thread_id}", thread_id_val or "default"
        )
        return Path(expanded).expanduser().resolve()

    def _resolve_mount_root(
        self, mount: WorkspaceMount, run_context: RunContext | None = None
    ) -> Path:
        return _call_root_provider(mount.root, self._get_scope(run_context))

    @staticmethod
    def _clean_rel(raw_path: str) -> str:
        clean = (raw_path or "").strip().lstrip("/\\")
        while clean.startswith("./") or clean.startswith(".\\"):
            clean = clean[2:].lstrip("/\\")
        return clean.replace("\\", "/")

    def _find_mount_by_prefix(self, prefix: str) -> WorkspaceMount | None:
        clean_prefix = prefix.strip("/")
        for mount in self.mounts:
            if mount.prefix.strip("/") == clean_prefix:
                return mount
        return None

    def _route_mount_path(
        self, raw_path: str
    ) -> tuple[WorkspaceMount | None, str, str | None]:
        clean = self._clean_rel(raw_path)
        parts = [p for p in clean.split("/") if p]
        if ".." in parts:
            return None, "", f"Security error: path {raw_path!r} escapes workspace root"

        head, _, tail = clean.partition("/")
        for mount in self.mounts:
            if head == mount.prefix.strip("/"):
                return mount, tail.lstrip("/"), None

        for mount in self.mounts:
            clean_aliases = tuple(a.strip("/\\") for a in mount.aliases)
            if head in clean_aliases:
                return mount, clean, None

        if self.default_mount:
            default = self._find_mount_by_prefix(self.default_mount)
            if default is not None:
                return default, clean, None

        prefixes = ", ".join(m.prefix.strip("/") for m in self.mounts)
        return None, "", f"Path {raw_path!r} must start with one of the mounted folders: {prefixes}"

    def _mounts_for_pattern(self, pattern: str) -> list[tuple[WorkspaceMount, str]]:
        clean = self._clean_rel(pattern) or "**/*"
        head, _, tail = clean.partition("/")
        for mount in self.mounts:
            if head == mount.prefix.strip("/"):
                return [(mount, tail.lstrip("/") or "**/*")]
        for mount in self.mounts:
            if head in tuple(a.strip("/\\") for a in mount.aliases):
                return [(mount, clean)]
        return [(mount, clean) for mount in self.mounts]

    def _is_hidden(self, path: Path, mount: WorkspaceMount | None = None) -> bool:
        hidden = mount.hidden_names if (mount and mount.hidden_names is not None) else self.hidden_names
        if path.name in hidden:
            return True
        return path.name.startswith(".") and path.name not in {".", ".."}

    def _is_blocked(
        self, path: Path, root: Path, mount: WorkspaceMount | None = None
    ) -> bool:
        """True when ``path`` is a credential file or blocked by mount policy."""
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            return False
        if rel == ".":
            return False
        if self.block_credentials and not any(fnmatch(rel, p) for p in self.allow_paths) and is_credential_path(rel):
            return True
        return bool(mount is not None and mount.is_path_blocked is not None and mount.is_path_blocked(rel))

    def _resolve_in_root(
        self,
        rel_path: str,
        root: Path,
        *,
        mount: WorkspaceMount | None = None,
        raw_display: str | None = None,
    ) -> tuple[Path | None, str | None]:
        clean = self._clean_rel(rel_path)
        parts = [p for p in clean.split("/") if p]
        if ".." in parts:
            return None, f"Security error: path {raw_display or rel_path!r} escapes workspace root {root}"

        resolver = mount.file_resolver if mount is not None else self.file_resolver
        target: Path | None = None
        if resolver is not None and clean and clean != ".":
            try:
                target = resolver(root, clean)
            except Exception as exc:
                return None, f"File resolver error: {exc}"

        if target is None:
            target = root if (not clean or clean == ".") else (root / clean)
        target = target.resolve()

        try:
            rel_posix = target.relative_to(root.resolve()).as_posix()
        except ValueError:
            return None, f"Security error: path {raw_display or rel_path!r} escapes workspace root {root}"

        if rel_posix != ".":
            if (
                self.block_credentials
                and not any(fnmatch(rel_posix, p) for p in self.allow_paths)
                and is_credential_path(rel_posix)
            ):
                return None, (
                    f"Access denied: {raw_display or rel_path!r} looks like a credential file. "
                    "Ask the operator to list it in `allow_paths` if the agent needs it."
                )
            if mount is not None and mount.is_path_blocked is not None and mount.is_path_blocked(rel_posix):
                return None, (
                    f"Security error: Access denied to internal path '{raw_display or rel_path}'."
                )

        return target, None

    def _resolve_target_path(self, rel_path: str, root: Path) -> tuple[Path | None, str | None]:
        return self._resolve_in_root(rel_path, root)

    def _resolve_path_any(
        self,
        raw_path: str,
        run_context: RunContext | None = None,
    ) -> tuple[WorkspaceMount | None, Path, str, Path | None, str | None]:
        """Resolve ``raw_path`` in either multi-mount mode or single-root mode.

        Returns ``(mount_or_None, root, display_prefix, target_path_or_None, error_or_None)``.
        """
        if not self.mounts:
            root = self.resolve_workspace_root(run_context).resolve()
            clean = self._clean_rel(raw_path)
            if not clean or clean == ".":
                return None, root, "", root, None
            target, err = self._resolve_in_root(clean, root, raw_display=raw_path)
            return None, root, "", target, err

        mount, sub_path, route_err = self._route_mount_path(raw_path)
        if route_err or mount is None:
            return None, Path(), "", None, route_err
        mount_root = self._resolve_mount_root(mount, run_context).resolve()
        prefix = mount.prefix.strip("/")
        target, err = self._resolve_in_root(
            sub_path, mount_root, mount=mount, raw_display=raw_path
        )
        return mount, mount_root, prefix, target, err

    @staticmethod
    def _format_display_path(target: Path, root: Path, display_prefix: str) -> str:
        try:
            rel = target.relative_to(root).as_posix()
        except ValueError:
            rel = target.name
        if not display_prefix:
            return rel
        return display_prefix if rel == "." else f"{display_prefix}/{rel}"

    def _render_mounts_root(self, run_context: RunContext | None = None) -> str:
        lines = [f"Mounted workspace folders ({len(self.mounts)}):"]
        for m in self.mounts:
            perm = "rw" if m.allow_write else "ro"
            prefix = m.prefix.strip("/")
            _ = self._resolve_mount_root(m, run_context)
            desc = f" — {m.description}" if m.description else ""
            lines.append(f"  - [{perm}] {prefix}/{desc}")
        return "\n".join(lines)

    def read(
        self,
        path: str = "",
        offset: int = 1,
        limit: int = 200,
        run_context: RunContext | None = None,
    ) -> str:
        """Read a file segment or inspect a directory in the workspace.

        Args:
            path: Relative path to the file or directory. Empty string or '.' reads root directory.
            offset: 1-indexed line number to start reading from (files only).
            limit: Maximum number of lines to return.
        """
        clean = self._clean_rel(path)
        if self.mounts and (not clean or clean == "."):
            return self._render_mounts_root(run_context)

        mount, root, prefix, target_path, err = self._resolve_path_any(path, run_context)
        if err:
            return f"Error: {err}"
        if not root.exists():
            return f"Workspace root directory does not exist yet: {root}"
        if target_path is None or not target_path.exists():
            return f"Error: Path '{path}' not found in workspace."

        target = target_path
        if target.is_dir():
            entries: list[str] = []
            try:
                children = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
            except OSError as exc:
                return f"Error reading directory '{path}': {exc}"

            for index, child in enumerate(children):
                if self.max_entries is not None and len(entries) >= self.max_entries:
                    entries.append(f"  ... ({len(children) - index} more items truncated)")
                    break
                if self._is_hidden(child, mount) or self._is_blocked(child, root, mount):
                    continue
                kind = "dir" if child.is_dir() else "file"
                rel_child = self._format_display_path(child, root, prefix)
                size_suffix = f" ({child.stat().st_size} bytes)" if child.is_file() else ""
                entries.append(f"  [{kind}] {rel_child}{size_suffix}")

            display_dir = self._format_display_path(target, root, prefix) or "."
            return f"Directory listing for '{display_dir}' ({len(entries)} items):\n" + "\n".join(
                entries
            )

        # Handle file
        try:
            target_stat = target.stat()
        except OSError as exc:
            return f"Error accessing '{path}': {exc}"

        display_path = self._format_display_path(target, root, prefix)
        mime, _ = mimetypes.guess_type(target.name)
        if mime and not (
            mime.startswith("text/")
            or mime in ("application/json", "application/xml", "application/javascript")
        ):
            try:
                with open(target, "rb") as f:
                    chunk = f.read(1024)
                    if b"\x00" in chunk:
                        return f"--- {display_path} (binary file, {target_stat.st_size} bytes) ---"
            except OSError:
                pass

        try:
            content = target.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            return f"Error reading text file '{path}': {exc}"

        lines = content.splitlines()
        total = len(lines)
        start_idx = max(0, offset - 1)
        end_idx = min(start_idx + limit, total)
        slice_lines = lines[start_idx:end_idx]
        formatted = "\n".join(slice_lines)
        end_line_num = min(offset + limit - 1, total)
        return f"--- {display_path} (lines {offset} to {end_line_num} of {total}) ---\n{formatted}"

    def glob(
        self,
        pattern: str = "*",
        base: str = "",
        run_context: RunContext | None = None,
    ) -> str:
        """List files matching a glob pattern in the workspace.

        Args:
            pattern: Glob pattern, e.g. '*.md', '**/*.json', or 'reports/*'.
            base: Optional relative subdirectory to search within.
        """
        base_clean = self._clean_rel(base)
        search_targets: list[tuple[WorkspaceMount | None, Path, str, Path, str]] = []
        if not self.mounts:
            root = self.resolve_workspace_root(run_context).resolve()
            if not root.exists():
                return "Workspace directory is empty."
            if base_clean and base_clean != ".":
                base_dir, err = self._resolve_in_root(base_clean, root, raw_display=base)
                if err:
                    return f"Error: {err}"
                if base_dir is None or not base_dir.exists() or not base_dir.is_dir():
                    return f"Error: Base directory '{base}' not found."
            else:
                base_dir = root
            search_targets.append((None, root, "", base_dir, pattern))
        elif base_clean and base_clean != ".":
            mount, root, prefix, base_dir, err = self._resolve_path_any(base, run_context)
            if err:
                return f"Error: {err}"
            if base_dir is None or not base_dir.exists() or not base_dir.is_dir():
                return f"Error: Base directory '{base}' not found."
            search_targets.append((mount, root, prefix, base_dir, pattern))
        else:
            for mount, sub_pattern in self._mounts_for_pattern(pattern):
                root = self._resolve_mount_root(mount, run_context).resolve()
                if root.exists():
                    search_targets.append((mount, root, mount.prefix.strip("/"), root, sub_pattern))

        matches: list[str] = []
        truncated = False
        try:
            for mount, root, prefix, base_dir, sub_pattern in search_targets:
                for matched in sorted(base_dir.glob(sub_pattern)):
                    resolved = matched.resolve()
                    try:
                        resolved.relative_to(root)
                    except ValueError:
                        continue
                    if self._is_hidden(resolved, mount) or self._is_blocked(resolved, root, mount):
                        continue
                    matches.append(self._format_display_path(resolved, root, prefix))
                    if self.max_matches is not None and len(matches) >= self.max_matches:
                        truncated = True
                        break
                if truncated:
                    break
        except Exception as exc:
            return f"Error globbing '{pattern}': {exc}"

        lines = [f"Found {len(matches)} item(s) matching '{pattern}':"]
        for m in matches:
            lines.append(f"  - {m}")
        if truncated:
            lines.append(f"  ... (results capped at {self.max_matches})")
        return "\n".join(lines)

    def grep(
        self,
        pattern: str,
        path: str = ".",
        is_regexp: bool = False,
        run_context: RunContext | None = None,
    ) -> str:
        """Search for a string or regex pattern within files in the workspace.

        Args:
            pattern: Text string or regular expression to match.
            path: Relative directory or file to search within (default: ".").
            is_regexp: True if pattern is a regular expression, False for literal substring search.
        """
        regex = None
        if is_regexp:
            try:
                regex = re.compile(pattern)
            except re.error as exc:
                return f"Invalid regex pattern '{pattern}': {exc}"

        clean_path = self._clean_rel(path)
        targets: list[tuple[WorkspaceMount | None, Path, str, Path]] = []
        if not self.mounts or (clean_path and clean_path != "."):
            mount, root, prefix, target_path, err = self._resolve_path_any(path, run_context)
            if err:
                return f"Error: {err}"
            if not root.exists():
                return "Workspace directory is empty."
            if target_path is None or not target_path.exists():
                return f"Error: Path '{path}' not found."
            targets.append((mount, root, prefix, target_path))
        else:
            for mount in self.mounts:
                root = self._resolve_mount_root(mount, run_context).resolve()
                if root.exists():
                    targets.append((mount, root, mount.prefix.strip("/"), root))

        matches: list[str] = []
        truncated = False
        for mount, root, prefix, target_path in targets:
            files: list[Path] = []
            if target_path.is_file():
                files = [target_path]
            else:
                try:
                    files = [
                        p
                        for p in sorted(target_path.rglob("*"))
                        if p.is_file()
                        and not self._is_hidden(p, mount)
                        and not self._is_blocked(p, root, mount)
                    ]
                except Exception as exc:
                    return f"Error traversing path '{path}': {exc}"

            for file_path in files:
                try:
                    content = file_path.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    continue

                rel_display = self._format_display_path(file_path, root, prefix)
                for line_no, line in enumerate(content.splitlines(), start=1):
                    hit = bool(regex.search(line)) if regex else (pattern in line)
                    if hit:
                        matches.append(f"  {rel_display}:{line_no}: {line}")
                        if self.max_matches is not None and len(matches) >= self.max_matches:
                            truncated = True
                            break
                if truncated:
                    break
            if truncated:
                break

        lines = [f"Found {len(matches)} match(es) for '{pattern}':"]
        lines.extend(matches)
        if truncated:
            lines.append(f"  ... (results capped at {self.max_matches})")
        return "\n".join(lines)

    def list_files(
        self,
        pattern: str = "**/*",
        run_context: RunContext | None = None,
    ) -> str:
        """List deliverable and source files in the current workspace.

        Args:
            pattern: Pattern to filter files (default: '**/*').
        """
        targets: list[tuple[WorkspaceMount | None, Path, str, str]] = []
        if not self.mounts:
            root = self.resolve_workspace_root(run_context).resolve()
            if not root.exists():
                return "Workspace directory is empty."
            targets.append((None, root, "", pattern))
        else:
            for mount, sub_pattern in self._mounts_for_pattern(pattern):
                root = self._resolve_mount_root(mount, run_context).resolve()
                if root.exists():
                    targets.append((mount, root, mount.prefix.strip("/"), sub_pattern))

        files: list[str] = []
        try:
            for opt_mount, root, prefix, sub_pattern in targets:
                for p in sorted(root.glob(sub_pattern)):
                    resolved = p.resolve()
                    try:
                        resolved.relative_to(root)
                    except ValueError:
                        continue
                    if (
                        p.is_file()
                        and not self._is_hidden(resolved, opt_mount)
                        and not self._is_blocked(resolved, root, opt_mount)
                    ):
                        files.append(self._format_display_path(resolved, root, prefix))
        except Exception as exc:
            return f"Error listing workspace files: {exc}"

        if not files:
            return f"No files matching '{pattern}' found in workspace."

        lines = [f"Available files in workspace ({len(files)} total):"]
        for f in files[: self.max_matches or len(files)]:
            lines.append(f"  - `{f}`")
        if self.max_matches is not None and len(files) > self.max_matches:
            lines.append(f"  ... ({len(files) - self.max_matches} more files)")
        return "\n".join(lines)

    def list_artifacts(self, run_context: RunContext | None = None) -> str:
        """List all generated deliverable artifacts (.md, .pptx, .html, .pdf, .json, etc.) in workspace."""
        valid_exts = {".md", ".pptx", ".html", ".pdf", ".json", ".csv", ".txt"}
        targets: list[tuple[WorkspaceMount | None, Path, str]] = []
        if not self.mounts:
            root = self.resolve_workspace_root(run_context).resolve()
            if not root.exists():
                return "No artifacts found in the current workspace."
            targets.append((None, root, ""))
        else:
            for mount in self.mounts:
                root = self._resolve_mount_root(mount, run_context).resolve()
                if root.exists():
                    targets.append((mount, root, mount.prefix.strip("/")))

        try:
            files: list[str] = []
            for opt_mount, root, prefix in targets:
                for p in sorted(root.rglob("*")):
                    if (
                        p.is_file()
                        and p.suffix.lower() in valid_exts
                        and not self._is_hidden(p, opt_mount)
                        and not self._is_blocked(p, root, opt_mount)
                    ):
                        files.append(self._format_display_path(p, root, prefix))
                        if self.max_matches and len(files) >= self.max_matches:
                            break
                if self.max_matches and len(files) >= self.max_matches:
                    break
            if not files:
                return "No artifacts found in the current workspace."
            lines = ["Available artifacts in workspace:"]
            for f in sorted(files):
                lines.append(f"  - `{f}`")
            return "\n".join(lines)
        except Exception as exc:
            return f"Error listing artifacts: {exc}"

    def diff_files(
        self,
        path_a: str,
        path_b: str,
        context_lines: int = 3,
        run_context: RunContext | None = None,
    ) -> str:
        """Return a unified diff between two files in the workspace.

        Args:
            path_a: Relative path to the first file.
            path_b: Relative path to the second file.
            context_lines: Unchanged context lines around each hunk (default 3).
        """
        _, _, _, target_a, err_a = self._resolve_path_any(path_a, run_context)
        if err_a:
            return f"Error: {err_a}"
        if target_a is None or not target_a.is_file():
            return f"Error: File '{path_a}' not found."

        _, _, _, target_b, err_b = self._resolve_path_any(path_b, run_context)
        if err_b:
            return f"Error: {err_b}"
        if target_b is None or not target_b.is_file():
            return f"Error: File '{path_b}' not found."

        text_a = target_a.read_text(encoding="utf-8", errors="replace")
        text_b = target_b.read_text(encoding="utf-8", errors="replace")
        if text_a == text_b:
            return f"No differences between '{path_a}' and '{path_b}'."

        diff_lines = list(
            difflib.unified_diff(
                text_a.splitlines(keepends=True),
                text_b.splitlines(keepends=True),
                fromfile=path_a.strip(),
                tofile=path_b.strip(),
                n=max(0, min(context_lines, 15)),
            )
        )
        rendered = "".join(diff_lines)
        if not rendered:
            return f"No differences between '{path_a}' and '{path_b}'."
        if len(rendered) > 12_000:
            rendered = rendered[:12_000] + f"\n... [diff truncated, {len(rendered) - 12_000} more chars]"
        return rendered

    # ------------------------------------------------------------------
    # Optional Write Tools (registered when allow_write or any mount is rw)
    # ------------------------------------------------------------------

    def write_file(
        self,
        path: str,
        content: str,
        mode: str = "write",
        reason: str = "",
        run_context: RunContext | None = None,
    ) -> str:
        """Write or append text content to a file in the workspace.

        Args:
            path: Relative path to the destination file.
            content: The text content to write.
            mode: "write" to overwrite (default) or "append" to append to the end.
            reason: Optional short explanation for audited mounts (such as playbook changes).
        """
        if not (self.allow_write or any(m.allow_write for m in self.mounts)):
            return "Error: File writing is disabled for this workspace."

        mount, root, prefix, target_path, err = self._resolve_path_any(path, run_context)
        if err:
            return f"Error: {err}"
        if mount is not None and not mount.allow_write:
            return f"Error: Mount '{prefix}' is read-only; writing to '{path}' is disabled."
        if target_path is None:
            return f"Error: Unable to resolve destination path '{path}'."

        rel_in_mount = target_path.relative_to(root).as_posix()
        if mount is not None and mount.write_handler is not None:
            return mount.write_handler(rel_in_mount, content, mode, reason, run_context)

        try:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            if mode == "append":
                with open(target_path, "a", encoding="utf-8") as f:
                    f.write(content)
            else:
                target_path.write_text(content, encoding="utf-8")

            if mount is not None and mount.on_write is not None:
                mount.on_write(rel_in_mount, target_path)
            display_path = self._format_display_path(target_path, root, prefix)
            return f"Successfully wrote {len(content)} characters to '{display_path}' (mode: {mode})."
        except Exception as exc:
            return f"Error writing to '{path}': {exc}"

    def patch_file(
        self,
        path: str,
        search_block: str,
        replace_block: str,
        reason: str = "",
        run_context: RunContext | None = None,
    ) -> str:
        """Replace a unique block of text in a workspace file (Search & Replace).

        Args:
            path: Relative path to the file.
            search_block: Exact text block to find (must match uniquely).
            replace_block: Replacement text block.
            reason: Optional short explanation for audited mounts (such as playbook changes).
        """
        if not (self.allow_write or any(m.allow_write for m in self.mounts)):
            return "Error: File editing is disabled for this workspace."

        mount, root, prefix, target_path, err = self._resolve_path_any(path, run_context)
        if err:
            return f"Error: {err}"
        if mount is not None and not mount.allow_write:
            return f"Error: Mount '{prefix}' is read-only; editing '{path}' is disabled."
        if target_path is None or not target_path.exists() or not target_path.is_file():
            return f"Error: File '{path}' not found."

        rel_in_mount = target_path.relative_to(root).as_posix()
        if mount is not None and mount.patch_handler is not None:
            return mount.patch_handler(
                rel_in_mount, search_block, replace_block, reason, run_context
            )

        try:
            content = target_path.read_text(encoding="utf-8")
        except Exception as exc:
            return f"Error reading '{path}': {exc}"

        count = content.count(search_block)
        if count == 0:
            return "Error: `search_block` was not found in file. Ensure exact matching of characters, whitespace, and indentation."
        if count > 1:
            return f"Error: `search_block` matched {count} times. Provide more surrounding context to ensure a unique match."

        updated = content.replace(search_block, replace_block, 1)
        try:
            target_path.write_text(updated, encoding="utf-8")
            if mount is not None and mount.on_write is not None:
                mount.on_write(rel_in_mount, target_path)
            display_path = self._format_display_path(target_path, root, prefix)
            return f"Successfully patched '{display_path}' (replaced 1 occurrence)."
        except Exception as exc:
            return f"Error writing update to '{path}': {exc}"
