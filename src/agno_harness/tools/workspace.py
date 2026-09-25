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

import logging
import mimetypes
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agno.tools.toolkit import Toolkit

from ..runtime.modules.streamui import resolve_artifact_dir
from ..runtime.scope import RunScope, current_scope

if TYPE_CHECKING:
    from agno.run import RunContext

logger = logging.getLogger(__name__)

_DEFAULT_HIDDEN_NAMES: frozenset[str] = frozenset(
    {".manifest.json", "__pycache__", ".DS_Store", ".git"}
)


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
    """

    def __init__(
        self,
        workspace_dir: str | os.PathLike[str] | Callable[[RunScope], Path | str] | None = None,
        *,
        file_resolver: Callable[[Path, str], Path | None] | None = None,
        allow_write: bool = False,
        max_entries: int = 100,
        max_matches: int = 50,
        name: str = "workspace",
        instructions: str | None = None,
        add_instructions: bool = True,
        hidden_names: frozenset[str] | None = None,
        **toolkit_kwargs: Any,
    ) -> None:
        self.workspace_dir = workspace_dir
        self.file_resolver = file_resolver
        self.allow_write = allow_write
        self.max_entries = max_entries
        self.max_matches = max_matches
        self.hidden_names = hidden_names if hidden_names is not None else _DEFAULT_HIDDEN_NAMES

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

        if self.allow_write:
            self.register(self.write_file, name="write_file")
            self.register(self.patch_file, name="patch_file")

    def _render_default_instructions(self) -> str:
        lines = [
            "You have access to safe workspace inspection tools (`read`, `glob`, `grep`, `list_files`) "
            "to explore existing files, reports, and code artifacts in the current workspace.",
            "- Use `list_files` or `glob` to discover available files.",
            "- Use `read` with `offset` and `limit` to inspect specific lines of a file without loading entire large files.",
            "- Use `grep` to quickly search keywords or regex across files.",
        ]
        if self.allow_write:
            lines.extend(
                [
                    "- Use `write_file` to write or append content to files.",
                    "- Use `patch_file` for precise Search & Replace modifications without overwriting entire files.",
                ]
            )
        else:
            lines.append(
                "NOTE: File modification tools are disabled in this environment. To create long documents, reports, or decks, "
                "stream them using `<stream-ui>` blocks in your response."
            )
        return "\n".join(lines)

    def resolve_workspace_root(self, run_context: RunContext | None = None) -> Path:
        """Resolve the active workspace root directory against current RunScope or RunContext."""
        scope = current_scope()
        if self.workspace_dir is not None:
            return resolve_artifact_dir(self.workspace_dir, scope)

        if scope is not None and scope.data.get("artifact_root_dir"):
            return resolve_artifact_dir(scope.data["artifact_root_dir"], scope)

        # Fallback using scope or run_context
        thread_id: str | None = None
        user_id: str | None = None
        if scope:
            thread_id = scope.thread_id
            user_id = scope.user_id

        if run_context:
            session_state = getattr(run_context, "session_state", None) or {}
            user_id = user_id or session_state.get("user_id")
            thread_id = (
                thread_id
                or session_state.get("thread_id")
                or getattr(run_context, "session_id", None)
            )

        template = "workspaces/{user_id}/{thread_id}"
        expanded = template.replace("{user_id}", user_id or "default").replace(
            "{thread_id}", thread_id or "default"
        )
        return Path(expanded).expanduser().resolve()

    def _is_hidden(self, path: Path) -> bool:
        if path.name in self.hidden_names:
            return True
        return path.name.startswith(".") and path.name not in {".", ".."}

    def _resolve_target_path(self, rel_path: str, root: Path) -> tuple[Path | None, str | None]:
        clean = rel_path.strip().lstrip("/\\")
        if clean.startswith("./"):
            clean = clean[2:].lstrip("/\\")

        target: Path | None = None
        if self.file_resolver is not None:
            try:
                target = self.file_resolver(root, clean)
            except Exception as exc:
                return None, f"File resolver error: {exc}"

        target = (root / clean).resolve() if target is None else target.resolve()

        try:
            target.relative_to(root)
        except ValueError:
            return None, f"Security error: path {rel_path!r} escapes workspace root {root}"

        return target, None

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
        root = self.resolve_workspace_root(run_context)
        if not root.exists():
            return f"Workspace root directory does not exist yet: {root}"

        clean = path.strip()
        if not clean or clean == ".":
            target = root
        else:
            target_path, err = self._resolve_target_path(clean, root)
            if err:
                return f"Error: {err}"
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
                if self.max_entries is not None and index >= self.max_entries:
                    entries.append(f"  ... ({len(children) - index} more items truncated)")
                    break
                if self._is_hidden(child):
                    continue
                kind = "dir" if child.is_dir() else "file"
                try:
                    rel_child = child.relative_to(root).as_posix()
                except ValueError:
                    rel_child = child.name
                size_suffix = f" ({child.stat().st_size} bytes)" if child.is_file() else ""
                entries.append(f"  [{kind}] {rel_child}{size_suffix}")

            try:
                display_dir = target.relative_to(root).as_posix() or "."
            except ValueError:
                display_dir = target.name
            return f"Directory listing for '{display_dir}' ({len(entries)} items):\n" + "\n".join(
                entries
            )

        # Handle file
        try:
            target_stat = target.stat()
        except OSError as exc:
            return f"Error accessing '{path}': {exc}"

        mime, _ = mimetypes.guess_type(target.name)
        if mime and not (
            mime.startswith("text/")
            or mime in ("application/json", "application/xml", "application/javascript")
        ):
            try:
                with open(target, "rb") as f:
                    chunk = f.read(1024)
                    if b"\x00" in chunk:
                        return f"--- {target.relative_to(root).as_posix()} (binary file, {target_stat.st_size} bytes) ---"
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
        display_path = target.relative_to(root).as_posix()
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
        root = self.resolve_workspace_root(run_context)
        if not root.exists():
            return "Workspace directory is empty."

        base_clean = base.strip()
        if base_clean and base_clean != ".":
            base_dir, err = self._resolve_target_path(base_clean, root)
            if err:
                return f"Error: {err}"
            if base_dir is None or not base_dir.exists() or not base_dir.is_dir():
                return f"Error: Base directory '{base}' not found."
        else:
            base_dir = root

        matches: list[str] = []
        truncated = False
        try:
            for matched in sorted(base_dir.glob(pattern)):
                resolved = matched.resolve()
                try:
                    resolved.relative_to(root)
                except ValueError:
                    continue
                if self._is_hidden(resolved):
                    continue
                matches.append(resolved.relative_to(root).as_posix())
                if self.max_matches is not None and len(matches) >= self.max_matches:
                    truncated = True
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
        root = self.resolve_workspace_root(run_context)
        if not root.exists():
            return "Workspace directory is empty."

        target_path, err = self._resolve_target_path(path, root)
        if err:
            return f"Error: {err}"
        if target_path is None or not target_path.exists():
            return f"Error: Path '{path}' not found."

        regex = None
        if is_regexp:
            try:
                regex = re.compile(pattern)
            except re.error as exc:
                return f"Invalid regex pattern '{pattern}': {exc}"

        files: list[Path] = []
        if target_path.is_file():
            files = [target_path]
        else:
            try:
                files = [
                    p
                    for p in sorted(target_path.rglob("*"))
                    if p.is_file() and not self._is_hidden(p)
                ]
            except Exception as exc:
                return f"Error traversing path '{path}': {exc}"

        matches: list[str] = []
        truncated = False
        for file_path in files:
            try:
                content = file_path.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue

            rel_display = file_path.relative_to(root).as_posix()
            for line_no, line in enumerate(content.splitlines(), start=1):
                hit = bool(regex.search(line)) if regex else (pattern in line)
                if hit:
                    matches.append(f"  {rel_display}:{line_no}: {line}")
                    if self.max_matches is not None and len(matches) >= self.max_matches:
                        truncated = True
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
        root = self.resolve_workspace_root(run_context)
        if not root.exists():
            return "Workspace directory is empty."

        try:
            files = [
                p.relative_to(root).as_posix()
                for p in sorted(root.glob(pattern))
                if p.is_file() and not self._is_hidden(p)
            ]
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

    # ------------------------------------------------------------------
    # Optional Write Tools (registered only when allow_write=True)
    # ------------------------------------------------------------------

    def write_file(
        self,
        path: str,
        content: str,
        mode: str = "write",
        run_context: RunContext | None = None,
    ) -> str:
        """Write or append text content to a file in the workspace.

        Args:
            path: Relative path to the destination file.
            content: The text content to write.
            mode: "write" to overwrite (default) or "append" to append to the end.
        """
        if not self.allow_write:
            return "Error: File writing is disabled for this workspace."

        root = self.resolve_workspace_root(run_context)
        target_path, err = self._resolve_target_path(path, root)
        if err:
            return f"Error: {err}"
        if target_path is None:
            return f"Error: Unable to resolve destination path '{path}'."

        try:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            if mode == "append":
                with open(target_path, "a", encoding="utf-8") as f:
                    f.write(content)
            else:
                target_path.write_text(content, encoding="utf-8")

            rel = target_path.relative_to(root).as_posix()
            return f"Successfully wrote {len(content)} characters to '{rel}' (mode: {mode})."
        except Exception as exc:
            return f"Error writing to '{path}': {exc}"

    def patch_file(
        self,
        path: str,
        search_block: str,
        replace_block: str,
        run_context: RunContext | None = None,
    ) -> str:
        """Replace a unique block of text in a workspace file (Search & Replace).

        Args:
            path: Relative path to the file.
            search_block: Exact text block to find (must match uniquely).
            replace_block: Replacement text block.
        """
        if not self.allow_write:
            return "Error: File editing is disabled for this workspace."

        root = self.resolve_workspace_root(run_context)
        target_path, err = self._resolve_target_path(path, root)
        if err:
            return f"Error: {err}"
        if target_path is None or not target_path.exists() or not target_path.is_file():
            return f"Error: File '{path}' not found."

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
            rel = target_path.relative_to(root).as_posix()
            return f"Successfully patched '{rel}' (replaced 1 occurrence)."
        except Exception as exc:
            return f"Error writing update to '{path}': {exc}"
