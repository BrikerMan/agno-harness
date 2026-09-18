"""Standard Todo & Task Planning Toolkit for agno-harness.

Provides multi-step task planning with a 2-level hierarchy (parent tasks and sub-tasks),
real-time progress tracking, dynamic state updates, and StreamUI card emission.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence
from typing import Any

from agno.tools import Toolkit, tool

from agno_harness.runtime.modules.streamui import emit_item, ui_block

# Markdown checkbox markers and their corresponding states.
TODO_MARKERS: dict[str, str] = {
    "": "pending",
    "-": "doing",
    "x": "done",
    "e": "error",
}

# Inverse lookup for serialization
STATE_TO_MARKER: dict[str, str] = {
    "pending": " ",
    "doing": "-",
    "done": "x",
    "error": "e",
}

_TODO_LINE = re.compile(r"^(\s*)[-*]\s*\[\s*(.?)\s*\]\s*(.+)$")


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
            return instance.__dict__.get(
                "instructions",
                ToolInstruction(self.fn(instance, instance.allow_subtasks)),
            )
        else:

            def _cls_call(allow_subtasks: bool = False) -> str:
                return self.fn(owner, allow_subtasks)

            return _cls_call


def parse_todo_list(text: str, allow_subtasks: bool = True) -> list[dict[str, Any]]:
    """Parse a markdown checklist into items the ``todo`` schema accepts.

    Parameters
    ----------
    text:
        Markdown checklist lines.
    allow_subtasks:
        Whether to parse indented lines as nested sub-tasks (level > 0).
        If False, all tasks are parsed as flat top-level tasks.

    Example::

        - [-] 1. Main task
          - [x] 1.1 Sub-task completed
          - [-] 1.2 Sub-task in progress
        - [ ] 2. Next main task
    """
    items: list[dict[str, Any]] = []
    for line in text.splitlines():
        match = _TODO_LINE.match(line)
        if match is None:
            continue
        indent, marker, label = match.group(1), match.group(2).lower(), match.group(3).strip()
        if label:
            item: dict[str, Any] = {
                "label": label,
                "state": TODO_MARKERS.get(marker, "pending"),
            }
            if allow_subtasks:
                indent_len = len(indent.replace("\t", "  "))
                level = indent_len // 2
                if level > 0:
                    item["level"] = level
            items.append(item)
    return items


def format_todo_list(items: Sequence[dict[str, Any]]) -> str:
    """Format structured items back into a standard markdown checklist."""
    lines: list[str] = []
    for item in items:
        level = item.get("level", 0)
        marker = STATE_TO_MARKER.get(item.get("state", "pending"), " ")
        prefix = "  " * level
        label = item.get("label", "")
        lines.append(f"{prefix}- [{marker}] {label}")
    return "\n".join(lines)


async def write_todo_plan(
    todos: str,
    title: str | None = None,
    allow_subtasks: bool = True,
) -> str:
    """Core implementation of writing and emitting a todo plan block."""
    items = parse_todo_list(todos, allow_subtasks=allow_subtasks)
    if not items:
        if allow_subtasks:
            return (
                "No tasks found. Write one markdown checkbox per line, e.g.:\n"
                "- [ ] 1. Main task\n"
                "  - [ ] 1.1 Sub-task"
            )
        return (
            "No tasks found. Write one markdown checkbox per line, e.g.:\n"
            "- [ ] 1. Task name\n"
            "- [ ] 2. Next task"
        )

    async with ui_block("todo-list", title=title):
        for item in items:
            await emit_item("todo", item)

    total = len(items)
    counts = Counter(item.get("state", "pending") for item in items)
    done = counts["done"]
    doing = counts["doing"]
    error = counts["error"]
    percent = round((done / total) * 100) if total > 0 else 0

    return (
        f"Plan updated: {total} tasks ({done} done, {doing} doing, {error} error) "
        f"— {percent}% complete."
    )


@tool
async def todo_write(todos: str, title: str | None = None) -> str:
    """Write or overwrite the multi-step task plan, displaying it on screen.

    Call this before starting work, and again as tasks or sub-tasks change state.
    Always send the full list: what you send replaces the current plan on screen.

    Supports a 2-level hierarchy: indent by 2 spaces for sub-tasks.

    Args:
        todos: The full plan formatted as a markdown checklist, e.g.::

            - [-] 1. Phase One
              - [x] 1.1 Setup environment
              - [-] 1.2 Run initialization
            - [ ] 2. Final verification

            Markers:
            - ``- [ ]``: Pending / not started
            - ``- [-]``: Doing / currently executing
            - ``- [x]``: Done / completed
            - ``- [e]``: Error / failed
        title: An optional title heading for the plan (e.g. "Workflow Plan").

    Returns:
        A one-line progress summary including task counts and completion percentage.
    """
    return await write_todo_plan(todos, title=title, allow_subtasks=True)


class TodoToolkit(Toolkit):
    """Standard multi-step task planning toolkit.

    Exposes ``todo_write`` to let the agent plan, track, and dynamically update
    a task list in real-time. Automatically injects its instructions into the Agent
    so the main agent instructions do not need manual editing.

    Parameters
    ----------
    default_title:
        Optional fallback title when the model does not pass one to ``todo_write``.
    allow_subtasks:
        Whether to enable nested sub-steps. Defaults to ``False`` (flat single-level checklist).
        Set to ``True`` to enable 2-level hierarchy with 2-space indented sub-tasks.
    allow_substeps:
        Alias for ``allow_subtasks``.
    add_instructions:
        Whether to automatically inject instructions into the parent Agent. Defaults to ``True``.
    """

    def __init__(
        self,
        default_title: str = "Task Execution Plan",
        allow_subtasks: bool = False,
        allow_substeps: bool | None = None,
        add_instructions: bool = True,
    ) -> None:
        if allow_substeps is not None:
            allow_subtasks = allow_substeps
        self.default_title = default_title
        self.allow_subtasks = allow_subtasks

        instruction_text = self._render_instructions(allow_subtasks)
        super().__init__(
            name="todo_toolkit",
            instructions=ToolInstruction(instruction_text),
            add_instructions=add_instructions,
        )

        self.register(self.todo_write)
        if "todo_write" in self.async_functions:
            if self.allow_subtasks:
                self.async_functions["todo_write"].description = (
                    "Write or replace the multi-step task plan on screen. "
                    "Always send the full list. Supports 2 levels: indent sub-tasks by 2 spaces."
                )
            else:
                self.async_functions["todo_write"].description = (
                    "Write or replace the multi-step task plan on screen. "
                    "Always send the full list. Keep a single flat checklist without indented sub-tasks."
                )

    async def todo_write(self, todos: str, title: str | None = None) -> str:
        """Write or replace the multi-step task plan on screen.

        Always send the full list: what you send replaces the current on-screen plan.

        Args:
            todos: Markdown checklist lines. ``- [ ]`` pending, ``- [-]`` doing,
                ``- [x]`` done, ``- [e]`` error.
            title: Title for the task plan. Defaults to the toolkit's default title.

        Returns:
            Current progress summary and percentage complete.
        """
        effective_title = title or self.default_title
        return await write_todo_plan(
            todos=todos,
            title=effective_title,
            allow_subtasks=self.allow_subtasks,
        )

    @classmethod
    def _render_instructions(cls, allow_subtasks: bool) -> str:
        if allow_subtasks:
            return """\
For any task requiring two or more steps, maintain a live plan with `todo_write`:
1. Call `todo_write` at the beginning with your planned steps before starting work.
2. Group complex steps with 2-space indented sub-tasks:
   - [-] 1. Data Pipeline
     - [x] 1.1 Download dataset
     - [-] 1.2 Process features
   - [ ] 2. Generate Report
3. As tasks transition (pending -> doing -> done), call `todo_write` with the full updated list.
4. CRITICAL RULE: When finishing your workflow, you MUST call `todo_write` one final time
   to mark ALL tasks and sub-tasks as completed `- [x]` BEFORE writing your final concluding text.
   Never leave tasks hanging in doing `[-]`.
"""
        return """\
For any task requiring two or more steps, maintain a live plan with `todo_write`:
1. Call `todo_write` at the beginning with your planned steps before starting work.
2. List steps clearly as a flat markdown checklist:
   - [-] 1. First step
   - [ ] 2. Next step
   - [ ] 3. Final step
   Keep the list flat without indented sub-tasks.
3. As tasks transition (pending -> doing -> done), call `todo_write` with the full updated list.
4. CRITICAL RULE: When finishing your workflow, you MUST call `todo_write` one final time
   to mark ALL tasks as completed `- [x]` BEFORE writing your final concluding text.
   Never leave tasks hanging in doing `[-]`.
"""

    @_InstructionsDescriptor
    def instructions(self_or_cls: Any, allow_subtasks: bool = False) -> str:
        """Standard system instructions for models using the Todo toolkit.

        Can be called on the class ``TodoToolkit.instructions(allow_subtasks=...)``
        or accessed on an instance ``toolkit.instructions`` / ``toolkit.instructions()``.
        """
        if isinstance(self_or_cls, type):
            return TodoToolkit._render_instructions(allow_subtasks)
        return self_or_cls._render_instructions(self_or_cls.allow_subtasks)


__all__ = [
    "STATE_TO_MARKER",
    "TODO_MARKERS",
    "TodoToolkit",
    "format_todo_list",
    "parse_todo_list",
    "todo_write",
]
