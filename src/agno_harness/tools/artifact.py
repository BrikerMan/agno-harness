"""Streaming Artifact Toolkit for agno-harness.

Provides standard streaming artifact/document card schema, guide instructions,
and agent maintenance tools (read_artifact_section, patch_artifact, append_artifact).
Avoids multi-minute UI freezes caused by traditional edit/write file tool calls
when generating long documents or code, and provides reliable Search & Replace diff repair.
"""

from __future__ import annotations

import asyncio
import difflib
import json
import os
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agno.tools import Toolkit, tool

from ..core.streamui import BlockSchema, ItemSchema
from ..runtime.modules.streamui import ui_block

if TYPE_CHECKING:
    from ..runtime.scope import RunScope


class ArtifactCard(BlockSchema):
    """A streaming long-form document, report, or file artifact.

    The model writes the body as raw Markdown without JSON escaping.
    """

    schema_name = "artifact"
    body = "text"

    title: str
    path: str | None = None
    summary: str | None = None
    mode: str | None = None


class SlideProgressItem(ItemSchema):
    """Progress event for a generated presentation slide."""

    schema_name = "slide_progress"
    page: int
    title: str | None = None


_SLIDE_PAGE_RE = re.compile(
    r"<!--\s*(?:SLIDE|PAGE|SLIDES\s+PAGE)\s*:\s*(\d+)(?:\s*[-–—:]\s*(.*?))?\s*-->",
    re.IGNORECASE,
)


class PresentationDeck(BlockSchema):
    """An HTML presentation deck generated in the text body.

    emit_text is False so the massive raw HTML (thousands of lines)
    does not flood the SSE stream. Instead, parse_line extracts page indicators
    (<!-- SLIDE: X - Title -->) so the frontend shows progress in real time.
    When the block ends, the full HTML in block.text is persisted to disk if
    artifact_root_dir is configured.
    """

    schema_name = "presentation_deck"
    body = "text"
    emit_text = False
    item = SlideProgressItem

    title: str
    filepath: str
    total_slides: int | None = None
    theme: str | None = None
    mode: str | None = None

    @classmethod
    def parse_line(cls, line: str, block: Any) -> Mapping[str, Any] | None:
        """Extract slide progress from HTML comments."""
        match = _SLIDE_PAGE_RE.search(line)
        if match:
            page = int(match.group(1))
            slide_title = match.group(2).strip() if match.group(2) else None
            return {
                "page": page,
                "title": slide_title,
            }
        return None


class ToolInstruction(str):
    """A string that can also be called like a method: instruction() -> str."""

    def __call__(self, *args: Any, **kwargs: Any) -> str:
        return str(self)


class _InstructionsDescriptor:
    """Descriptor enabling .instructions to work as attribute, method, or classmethod."""

    def __init__(self, fn: Any) -> None:
        self.fn = fn

    def __get__(self, instance: Any, owner: Any) -> Any:
        if instance is not None:
            return instance.__dict__.get("instructions", ToolInstruction(self.fn(instance)))
        else:

            def _cls_call() -> str:
                return self.fn(owner)

            return _cls_call


async def emit_artifact(
    title: str,
    content: str,
    *,
    path: str | None = None,
    summary: str | None = None,
    chunk_size: int = 200,
    delay_s: float = 0.0,
) -> None:
    """Stream a long-form document or artifact to the UI via the side channel.

    Parameters
    ----------
    title:
        Title of the document or report.
    content:
        The markdown content of the document.
    path:
        Optional file path (e.g. 'reports/summary.md').
    summary:
        Optional one-line summary of the document.
    chunk_size:
        Characters per streamed chunk.
    delay_s:
        Optional delay between chunks in seconds to allow smooth streaming.
    """
    props: dict[str, Any] = {"title": title}
    if path is not None:
        props["path"] = path
    if summary is not None:
        props["summary"] = summary

    async with ui_block(ArtifactCard.schema_name, **props) as block:
        if content:
            for i in range(0, len(content), chunk_size):
                await block.text(content[i : i + chunk_size])
                if delay_s > 0:
                    await asyncio.sleep(delay_s)


def _resolve_artifact_path(
    filepath: str,
    configured_root: str | os.PathLike[str] | Callable[[RunScope], Path | str] | None = None,
) -> tuple[Path | None, str | None]:
    """Resolve an artifact relative path against the active artifact root directory."""
    from ..runtime.modules.streamui import resolve_artifact_dir
    from ..runtime.scope import current_scope

    scope = current_scope()
    root = configured_root
    if root is None and scope is not None:
        root = scope.data.get("artifact_root_dir")
    if root is None:
        thread_part = scope.thread_id if scope is not None else "default"
        root = Path("data/artifacts") / thread_part

    root_dir = resolve_artifact_dir(root, scope)
    clean_path = filepath.strip().lstrip("/\\")
    if clean_path.startswith("./"):
        clean_path = clean_path[2:].lstrip("/\\")
    if root_dir.name == "artifacts" and (
        clean_path.startswith("artifacts/") or clean_path.startswith("artifacts\\")
    ):
        clean_path = clean_path[len("artifacts/") :].lstrip("/\\")
    target_path = (root_dir / clean_path).resolve()
    if not target_path.is_relative_to(root_dir):
        return None, f"Security error: path {filepath!r} escapes artifact directory {root_dir}"
    return target_path, None


def _read_artifact_section(
    filepath: str,
    start_line: int | None = None,
    line_count: int = 50,
    query: str | None = None,
    *,
    configured_root: Any = None,
) -> str:
    """Read a targeted section or search within an existing generated artifact."""
    target_path, err = _resolve_artifact_path(filepath, configured_root)
    if err:
        return json.dumps(
            {"status": "error", "filepath": filepath, "error": err}, ensure_ascii=False
        )
    if target_path is None or not target_path.exists():
        return json.dumps(
            {
                "status": "error",
                "filepath": filepath,
                "error": f"File '{filepath}' not found in artifact directory.",
            },
            ensure_ascii=False,
            indent=2,
        )

    try:
        content = target_path.read_text(encoding="utf-8")
    except Exception as exc:
        return json.dumps(
            {"status": "error", "filepath": filepath, "error": f"Failed to read file: {exc}"},
            ensure_ascii=False,
            indent=2,
        )

    lines = content.splitlines()
    total_lines = len(lines)

    matched_lines: list[int] = []
    if query:
        query_lower = query.lower()
        for idx, line in enumerate(lines, start=1):
            if query_lower in line.lower():
                matched_lines.append(idx)

    if start_line is None:
        start_line = max(1, matched_lines[0] - 5) if matched_lines else 1

    start_line = max(1, min(start_line, total_lines or 1))
    line_count = max(1, min(line_count, 200))
    end_line = min(total_lines, start_line + line_count - 1)

    window = lines[start_line - 1 : end_line]
    formatted_content = "\n".join(f"{start_line + i:4d} | {line}" for i, line in enumerate(window))

    result = {
        "status": "success",
        "filepath": filepath,
        "query": query,
        "start_line": start_line,
        "end_line": end_line,
        "total_lines": total_lines,
        "has_more": end_line < total_lines,
        "match_count": len(matched_lines),
        "matched_lines": matched_lines[:20] if matched_lines else [],
        "content": formatted_content,
    }
    return json.dumps(result, ensure_ascii=False, indent=2)


async def _patch_artifact(
    filepath: str,
    search_block: str,
    replace_block: str,
    title: str | None = None,
    *,
    configured_root: Any = None,
) -> str:
    """Safely apply a Search & Replace patch to a persisted artifact file."""
    target_path, err = _resolve_artifact_path(filepath, configured_root)
    if err:
        return json.dumps(
            {"status": "error", "filepath": filepath, "error": err}, ensure_ascii=False
        )
    if target_path is None or not target_path.exists():
        return json.dumps(
            {
                "status": "error",
                "filepath": filepath,
                "error": f"File '{filepath}' not found in artifact directory.",
            },
            ensure_ascii=False,
            indent=2,
        )

    try:
        old_content = target_path.read_text(encoding="utf-8")
    except Exception as exc:
        return json.dumps(
            {"status": "error", "filepath": filepath, "error": f"Failed to read file: {exc}"},
            ensure_ascii=False,
            indent=2,
        )

    count = old_content.count(search_block)
    match_index = -1
    matched_search = search_block

    if count == 1:
        match_index = old_content.find(search_block)
    elif count > 1:
        lines = old_content.splitlines(keepends=True)
        search_first_line = search_block.splitlines()[0] if search_block.splitlines() else ""
        candidate_lines = [
            i + 1
            for i, line in enumerate(lines)
            if search_first_line and search_first_line.strip() in line
        ]
        return json.dumps(
            {
                "status": "error",
                "filepath": filepath,
                "error": f"search_block matched {count} times in file. Provide more surrounding context lines to make the match unique.",
                "candidate_lines": candidate_lines[:10],
            },
            ensure_ascii=False,
            indent=2,
        )
    else:
        # Fallback: line-by-line whitespace-trimmed search
        lines = old_content.splitlines(keepends=True)
        search_lines = search_block.splitlines()
        for i in range(len(lines) - len(search_lines) + 1):
            chunk = [lines[i + j].rstrip("\r\n").rstrip() for j in range(len(search_lines))]
            if chunk == [sl.rstrip() for sl in search_lines]:
                match_index = sum(len(lines[k]) for k in range(i))
                matched_search = "".join(lines[i : i + len(search_lines)])
                break

    if match_index < 0:
        return json.dumps(
            {
                "status": "error",
                "filepath": filepath,
                "error": "search_block was not found in file. Use read_artifact_section to inspect the exact lines.",
                "hint": "Ensure exact matching of lines, indentation, and punctuation.",
            },
            ensure_ascii=False,
            indent=2,
        )

    new_content = (
        old_content[:match_index] + replace_block + old_content[match_index + len(matched_search) :]
    )

    start_line = old_content[:match_index].count("\n") + 1
    lines_removed = len(matched_search.splitlines())
    lines_added = len(replace_block.splitlines())
    end_line = start_line + max(0, lines_removed - 1)

    diff_lines = list(
        difflib.unified_diff(
            matched_search.splitlines(keepends=True),
            replace_block.splitlines(keepends=True),
            fromfile=f"a/{filepath}",
            tofile=f"b/{filepath}",
            n=2,
        )
    )
    diff_text = "".join(diff_lines)

    target_path.write_text(new_content, encoding="utf-8")

    try:
        async with ui_block("diff", path=filepath, title=title or f"Patch {filepath}") as block:
            await block.text(diff_text)
    except Exception:
        pass

    result = {
        "status": "success",
        "action": "patch",
        "filepath": filepath,
        "saved_path": str(target_path),
        "replaced_lines_span": [start_line, end_line],
        "lines_added": lines_added,
        "lines_removed": lines_removed,
        "file_size_bytes": target_path.stat().st_size,
        "total_lines": len(new_content.splitlines()),
        "summary": (
            f"Successfully patched {filepath}: replaced lines {start_line}-{end_line} "
            f"(-{lines_removed}/+{lines_added} lines)."
        ),
    }
    return json.dumps(result, ensure_ascii=False, indent=2)


async def _append_artifact(
    filepath: str,
    content: str,
    *,
    configured_root: Any = None,
) -> str:
    """Append additional content (such as remaining slides or sections) to an incomplete artifact."""
    target_path, err = _resolve_artifact_path(filepath, configured_root)
    if err:
        return json.dumps(
            {"status": "error", "filepath": filepath, "error": err}, ensure_ascii=False
        )
    if target_path is None:
        return json.dumps(
            {"status": "error", "filepath": filepath, "error": "Unable to resolve target path."},
            ensure_ascii=False,
            indent=2,
        )

    target_path.parent.mkdir(parents=True, exist_ok=True)
    with target_path.open("a", encoding="utf-8") as f:
        f.write(content)

    full_content = target_path.read_text(encoding="utf-8")
    is_html = filepath.lower().endswith((".html", ".htm"))
    is_complete_html = ("</html>" in full_content.lower()) if is_html else None
    slide_matches = _SLIDE_PAGE_RE.findall(full_content)

    result = {
        "status": "success",
        "action": "append",
        "filepath": filepath,
        "saved_path": str(target_path),
        "appended_bytes": len(content.encode("utf-8")),
        "appended_lines": len(content.splitlines()),
        "total_bytes": target_path.stat().st_size,
        "total_lines": len(full_content.splitlines()),
        "is_complete_html": is_complete_html,
        "slide_count": len(slide_matches) if slide_matches else None,
        "summary": f"Appended {len(content)} characters ({len(content.splitlines())} lines) to {filepath}.",
    }
    return json.dumps(result, ensure_ascii=False, indent=2)


async def _stream_artifact(
    title: str,
    content: str,
    path: str | None = None,
    summary: str | None = None,
) -> str:
    await emit_artifact(title, content, path=path, summary=summary)
    lines = len(content.splitlines()) if content else 0
    chars = len(content) if content else 0
    return f"Artifact {title!r} streamed to UI ({lines} lines, {chars} characters)."


class _CallableTool:
    """A wrapper that allows a tool to be both passed to Agno and called directly."""

    def __init__(self, fn: Any) -> None:
        self._fn = fn
        self._tool = tool(fn)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self._fn(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._tool, name)


stream_artifact = _CallableTool(_stream_artifact)
read_artifact_section = _CallableTool(_read_artifact_section)
patch_artifact = _CallableTool(_patch_artifact)
append_artifact = _CallableTool(_append_artifact)


class StreamingArtifactToolkit(Toolkit):
    """Toolkit for streaming long documents, reports, and code artifacts.

    Enforces that long text is emitted as a stream-ui artifact block rather than
    calling edit/write tools that freeze the UI. Also provides targeted inspection
    and Search & Replace diff tools for safe subsequent edits.
    """

    TOOL_NAME = "stream_artifact"

    instructions = _InstructionsDescriptor(lambda self: self._render_instructions())

    def __init__(
        self,
        *,
        artifact_root_dir: str | os.PathLike[str] | Callable[[RunScope], Path | str] | None = None,
        include_stream_tool: bool = False,
        add_instructions: bool = True,
    ) -> None:
        self.artifact_root_dir = artifact_root_dir
        self.include_stream_tool = include_stream_tool
        instruction_text = self._render_instructions()
        super().__init__(
            name="streaming_artifact_toolkit",
            instructions=ToolInstruction(instruction_text),
            add_instructions=add_instructions,
        )
        self.register(self.read_artifact_section)
        self.register(self.patch_artifact)
        self.register(self.append_artifact)
        if self.include_stream_tool:
            self.register(self.stream_artifact)

    def read_artifact_section(
        self,
        filepath: str,
        start_line: int | None = None,
        line_count: int = 50,
        query: str | None = None,
    ) -> str:
        """Read a targeted section or search within an existing generated artifact.

        Args:
            filepath: Relative path of the file (e.g. 'output/deck.html' or 'reports/summary.md').
            start_line: 1-indexed line number to start reading from.
            line_count: Number of lines to return (default 50).
            query: Optional keyword or substring to search for. If provided, centers the view on the match.

        Returns:
            JSON string containing line-numbered content and metadata.
        """
        return _read_artifact_section(
            filepath=filepath,
            start_line=start_line,
            line_count=line_count,
            query=query,
            configured_root=self.artifact_root_dir,
        )

    async def patch_artifact(
        self,
        filepath: str,
        search_block: str,
        replace_block: str,
        title: str | None = None,
    ) -> str:
        """Safely apply a Search & Replace patch to a persisted artifact file.

        Args:
            filepath: Relative path of the file to modify.
            search_block: Exact block of lines from the file to find and replace.
            replace_block: New replacement content for search_block.
            title: Optional short description for the visual diff card.

        Returns:
            JSON string containing patch status, replaced line span, and diff summary.
        """
        return await _patch_artifact(
            filepath=filepath,
            search_block=search_block,
            replace_block=replace_block,
            title=title,
            configured_root=self.artifact_root_dir,
        )

    async def append_artifact(
        self,
        filepath: str,
        content: str,
    ) -> str:
        """Append additional content (such as remaining slides or sections) to an incomplete artifact.

        Args:
            filepath: Relative path of the file to append to.
            content: The additional content to append.

        Returns:
            JSON string containing append status, byte counts, and completeness.
        """
        return await _append_artifact(
            filepath=filepath,
            content=content,
            configured_root=self.artifact_root_dir,
        )

    async def stream_artifact(
        self,
        title: str,
        content: str,
        path: str | None = None,
        summary: str | None = None,
    ) -> str:
        """Stream a long-form document or artifact to the UI.

        Args:
            title: Title of the document or report.
            content: The markdown content of the document.
            path: Optional file path for the document.
            summary: Optional brief summary.

        Returns:
            Status confirmation with line and character counts.
        """
        return await stream_artifact(
            title=title,
            content=content,
            path=path,
            summary=summary,
        )

    @classmethod
    def _render_instructions(cls) -> str:
        return """\
When generating or modifying long-form documents, reports, proposals, full-file code, or presentation decks:
1. NEVER call traditional file write or edit tools (such as `write_file` or `edit_file`) with massive content. Putting thousands of lines inside an unclosed JSON argument blocks the stream and causes a multi-minute UI freeze.
2. For long documents, reports, or proposals (Markdown/text), output directly using the `artifact` StreamUI block:
   <stream-ui>
   {"schema": "artifact", "title": "Report Title", "path": "reports/summary.md"}
   # Report Title
   Content goes here in standard Markdown...
   ```python
   def example():
       return "Freely include code blocks here without parsing conflicts"
   ```
   </stream-ui>
3. For multi-slide HTML presentations or decks, output directly using the `presentation_deck` StreamUI block:
   <stream-ui>
   {"schema": "presentation_deck", "title": "Deck Title", "filepath": "output/deck.html", "total_slides": 5}
   <!DOCTYPE html>
   <html>
   ...
   <!-- SLIDE: 1 - Title Slide -->
   <section class="slide">...</section>
   <!-- SLIDE: 2 - Architecture Overview -->
   <section class="slide">...</section>
   ...
   </html>
   </stream-ui>
   Always include `<!-- SLIDE: X - Title -->` comments before each slide section so real-time progress is rendered on the user's card.
4. Continuing an interrupted/truncated generation:
   If an artifact was cut off mid-stream, DO NOT regenerate the entire file from the beginning.
   Instead, call `append_artifact(filepath, content)` or stream with `{"schema": "...", "mode": "append"}` to resume from where it stopped.
5. Inspecting before editing:
   To inspect lines in an existing artifact, call `read_artifact_section(filepath, start_line, line_count, query)`. Never dump the entire 3000-line file into your prompt context.
6. Modifying an existing file (1 -> N edits):
   NEVER rewrite the whole file from scratch. Use `patch_artifact(filepath, search_block, replace_block)` with exact Search & Replace blocks. This is fast, token-efficient, and automatically renders a visual Diff card in the chat.
"""


__all__ = [
    "ArtifactCard",
    "PresentationDeck",
    "SlideProgressItem",
    "StreamingArtifactToolkit",
    "append_artifact",
    "emit_artifact",
    "patch_artifact",
    "read_artifact_section",
    "stream_artifact",
]
