"""The StreamUI module: cards out of the text stream, and out of tools.

Owns the ``ui.`` custom-event namespace. Parsing lives in
:mod:`agno_harness.core.streamui`, which knows nothing about runs; this is
the part that gives it a place in the pipeline, a per-run lifetime, and the two
things parsing cannot do on its own — validate against the catalog, and await a
resolver.

TWO PRODUCERS, ONE FORMAT
-------------------------
Cards arrive from two directions. The model writes a fence, which is how it
presents what it chose. A tool calls :func:`ui_block` / :func:`emit_item`, which
is how search results appear before the summary that discusses them and how a
long tool reports progress while it is still running. Both go through the same
catalog, the same validation and the same ``ui.*`` events, so the frontend has
one thing to render and one thing to test.

WHY VALIDATION HAPPENS HERE AND NOT IN THE PARSER
-------------------------------------------------
Because a resolver is an ``await``. The model writes ``{"id": 123}`` and the
server turns that into a title, a poster and a rating by asking the source of
truth. That call is asynchronous, which is why ``BridgeModule.stage`` is an
async iterator rather than a plain one.

A resolver that fails does not remove the card. The item is emitted with
``resolveError`` and renders in a degraded form, matching the rule that already
governs a malformed line: one bad part of a block never destroys the block.
"""

from __future__ import annotations

import contextlib
import json
import os
import uuid
from collections.abc import AsyncIterator, Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from ag_ui.core import BaseEvent, CustomEvent, EventType, TextMessageContentEvent

from ...core.streamui import (
    EVENT_BLOCK_END,
    EVENT_BLOCK_START,
    EVENT_ITEM,
    EVENT_TEXT,
    CardCatalog,
    StreamUIBlock,
    StreamUIEvent,
    StreamUIFenceParser,
)
from ..module import ChunkConverter, Module
from ..scope import RunScope
from ..sidechannel import current_channel

NAMESPACE = "ui."


class UISignal(StrEnum):
    BLOCK_START = "block.start"
    ITEM = "item"
    TEXT = "text"
    BLOCK_END = "block.end"


@dataclass
class UIItem:
    """A card fragment a tool put on the side channel."""

    signal: UISignal
    block_id: str
    schema: str | None = None
    props: dict[str, Any] = field(default_factory=dict)
    data: Any = None
    delta: str = ""
    total: int = 0

    @property
    def bracket(self) -> int:
        """Tool-side blocks do not hold the parent's chunks.

        Unlike a delegation, a card block carries no frames that could be
        confused with the parent's: every event inside it is addressed to a
        block id. Holding the parent back would only delay its prose for no
        gain.
        """
        return 0


class _OpenBlock:
    """The handle a tool holds while its block is open."""

    def __init__(self, block_id: str, schema: str, channel: Any) -> None:
        self.block_id = block_id
        self.schema = schema
        self._channel = channel
        self._count = 0

    async def item(self, schema: str, data: Mapping[str, Any] | None = None) -> None:
        self._count += 1
        if self._channel is None:
            return
        await self._channel.put(
            UIItem(
                signal=UISignal.ITEM,
                block_id=self.block_id,
                schema=schema,
                data=dict(data or {}),
            )
        )

    async def text(self, delta: str) -> None:
        if self._channel is None:
            return
        await self._channel.put(UIItem(signal=UISignal.TEXT, block_id=self.block_id, delta=delta))

    @property
    def count(self) -> int:
        return self._count


_CURRENT_BLOCK: ContextVar[_OpenBlock | None] = ContextVar(
    "better_agno_streamui_block", default=None
)


@contextlib.asynccontextmanager
async def ui_block(schema: str, **props: Any) -> AsyncIterator[_OpenBlock]:
    """Open a card block from inside a tool.

    Outside a run — in a test, a script, a non-streaming call — this is a no-op
    and the tool behaves exactly as it otherwise would, which is the property
    that keeps ``substream`` usable in the same places.
    """
    channel = current_channel()
    if channel is not None and not getattr(channel, "enabled", True):
        channel = None
    block = _OpenBlock(f"ui-{uuid.uuid4().hex[:12]}", schema, channel)

    if channel is not None:
        await channel.put(
            UIItem(
                signal=UISignal.BLOCK_START,
                block_id=block.block_id,
                schema=schema,
                props=dict(props),
            )
        )
    token = _CURRENT_BLOCK.set(block)
    try:
        yield block
    finally:
        _CURRENT_BLOCK.reset(token)
        if channel is not None:
            # Always close the bracket, including when the tool raises.
            await channel.put(
                UIItem(
                    signal=UISignal.BLOCK_END,
                    block_id=block.block_id,
                    total=block.count,
                )
            )


async def emit_item(schema: str, data: Mapping[str, Any] | None = None) -> None:
    """Append one item to the block opened by the enclosing :func:`ui_block`."""
    block = _CURRENT_BLOCK.get()
    if block is None:
        return
    await block.item(schema, data)


async def emit_text(delta: str) -> None:
    """Append raw text to the block opened by the enclosing :func:`ui_block`."""
    block = _CURRENT_BLOCK.get()
    if block is None:
        return
    await block.text(delta)


def resolve_artifact_dir(
    root: str | os.PathLike[str] | Callable[[RunScope], Path | str],
    scope: RunScope | None = None,
) -> Path:
    """Resolve an artifact root directory against the current run scope.

    Supports dynamic template variables such as ``{task-id}``, ``{task_id}``,
    ``{thread-id}``, ``{thread_id}``, ``{run-id}``, ``{run_id}``, ``{user-id}``,
    ``{user_id}``, and expands user home directory ``~``.
    """
    if callable(root):
        resolved = root(scope) if scope is not None else root(None)  # type: ignore[arg-type]
        return Path(resolved).expanduser().resolve()

    raw = str(root)
    expanded = os.path.expanduser(raw)
    thread_id = scope.thread_id if scope else "default"
    run_id = scope.run_id if scope else "default"
    user_id = (scope.user_id if scope else None) or "default"
    subs = {
        "task_id": thread_id,
        "task-id": thread_id,
        "thread_id": thread_id,
        "thread-id": thread_id,
        "run_id": run_id,
        "run-id": run_id,
        "user_id": user_id,
        "user-id": user_id,
    }
    for key, val in subs.items():
        expanded = expanded.replace(f"{{{key}}}", str(val))
    return Path(expanded).resolve()


class StreamUIModule(Module):
    """Turns ``stream-ui`` fences and tool emissions into ``ui.*`` events.

    Parameters
    ----------
    catalog:
        The registered card schemas. Without one nothing is validated and no
        resolver runs — blocks pass through with whatever the model wrote, which
        is a usable prototyping mode but not one to ship.
    enabled:
        When off the module is a pass-through, so it can stay registered and the
        pipeline's shape does not change with configuration.
    artifact_root_dir:
        Base directory or template (e.g. '~/artifacts/{task-id}') where blocks with
        a 'filepath' or 'filename' are automatically persisted upon block completion.
    """

    name: str = "streamui"
    namespace: str | None = NAMESPACE

    def __init__(
        self,
        catalog: CardCatalog | None = None,
        *,
        enabled: bool = True,
        artifact_root_dir: str | os.PathLike[str] | Callable[[RunScope], Path | str] | None = None,
    ) -> None:
        self.catalog = catalog
        self.enabled = enabled
        self.artifact_root_dir = artifact_root_dir

    # ── the fence path ────────────────────────────────────────────────────

    def _parser_for(self, run: RunScope, message_id: str) -> StreamUIFenceParser:
        parsers: dict[str, StreamUIFenceParser] = self.data(run).setdefault("parsers", {})
        parser = parsers.get(message_id)
        if parser is None:
            parser = parsers[message_id] = StreamUIFenceParser(self.catalog)
        return parser

    async def stage(self, event: BaseEvent, run: RunScope) -> AsyncIterator[BaseEvent]:
        if not self.enabled:
            yield event
            return

        etype = getattr(event, "type", None)
        message_id = getattr(event, "message_id", "") or ""

        if etype is EventType.TEXT_MESSAGE_START:
            self._parser_for(run, message_id)
            yield event
            return

        if etype is EventType.TEXT_MESSAGE_CONTENT:
            parser = self._parser_for(run, message_id)
            visible, produced = parser.feed(getattr(event, "delta", "") or "")
            # Visible text first: within one delta the fence always follows the
            # text before it, and emitting in source order means the client
            # opens the message before the blocks that belong to it.
            if visible:
                yield event.model_copy(update={"delta": visible})
            async for out in self._convert(produced, run, message_id):
                yield out
            return

        if etype is EventType.TEXT_MESSAGE_END:
            parser = self.data(run).get("parsers", {}).pop(message_id, None)
            if parser is not None:
                visible, produced = parser.flush()
                async for out in self._convert(produced, run, message_id):
                    yield out
                if visible:
                    yield TextMessageContentEvent(
                        type=EventType.TEXT_MESSAGE_CONTENT,
                        message_id=message_id,
                        delta=visible,
                    )
            yield event
            return

        yield event

    async def _convert(
        self, produced: list[StreamUIEvent], run: RunScope, message_id: str | None
    ) -> AsyncIterator[BaseEvent]:
        for parsed in produced:
            value = dict(parsed.payload)
            if message_id:
                value["messageId"] = message_id
            if parsed.kind == EVENT_BLOCK_START:
                self._validate_props(value)
                self._open_blocks(run).append(parsed.block_id)
                parser = self.data(run).get("parsers", {}).get(message_id)
                if parser is not None:
                    block = parser.get_block(parsed.block_id)
                    if block is not None:
                        self.data(run).setdefault("blocks", {})[parsed.block_id] = block
            elif parsed.kind == EVENT_ITEM:
                await self._finish_item(value)
            elif parsed.kind == EVENT_BLOCK_END:
                await self._finish_block(value, parsed.block_id, run)
                self._close_block(run, parsed.block_id)
            yield CustomEvent(type=EventType.CUSTOM, name=parsed.kind, value=value)

    # ── validation and resolution, shared by both producers ───────────────

    def _validate_props(self, value: dict[str, Any]) -> None:
        schema = value.get("schema")
        if self.catalog is None or value.get("error") or not isinstance(schema, str):
            return
        props, error = self.catalog.validate_props(schema, value.get("props") or {})
        value["props"] = props
        if error:
            value["error"] = error

    async def _finish_item(self, value: dict[str, Any]) -> None:
        """Validate one item and, if a resolver is registered, enrich it."""
        if self.catalog is None or value.get("error"):
            return
        schema = value.get("schema")
        if not isinstance(schema, str):
            return

        data, error = self.catalog.validate_item(schema, value.get("data"))
        if error:
            value.pop("data", None)
            value["raw"] = value.get("raw") or _compact(value.get("data"))
            value["error"] = error
            return
        value["data"] = data

        if not self.catalog.has_resolver(schema):
            return
        try:
            resolved = await self.catalog.resolve(schema, data)
        except Exception as exc:
            # Degrade, do not disappear: the client shows the card with what the
            # model supplied and an indication that the details are missing.
            value["resolveError"] = f"{type(exc).__name__}: {exc}"
            return
        if resolved is not None:
            value["resolved"] = dict(resolved)

    async def _finish_block(self, value: dict[str, Any], block_id: str, run: RunScope) -> None:
        block = self.data(run).get("blocks", {}).get(block_id)
        schema = block.schema if block else None

        if self.catalog is not None and schema:
            try:
                res = await self.catalog.complete(schema, block, run)
                if isinstance(res, Mapping):
                    value.update(res)
            except Exception as exc:
                value["completionError"] = f"{type(exc).__name__}: {exc}"

        if (
            block
            and block.text
            and self.catalog is not None
            and schema
            and not self.catalog.should_emit_text(schema)
        ):
            value["text"] = block.text

        if self.artifact_root_dir is not None and block and block.text:
            rel_path = (
                block.props.get("filepath")
                or block.props.get("filename")
                or block.props.get("path")
            )
            if rel_path and isinstance(rel_path, str):
                try:
                    root_path = resolve_artifact_dir(self.artifact_root_dir, run)
                    clean_rel = rel_path.strip().lstrip("/\\")
                    if clean_rel.startswith("./"):
                        clean_rel = clean_rel[2:].lstrip("/\\")
                    if root_path.name == "artifacts" and (
                        clean_rel.startswith("artifacts/") or clean_rel.startswith("artifacts\\")
                    ):
                        clean_rel = clean_rel[len("artifacts/") :].lstrip("/\\")
                    target_path = (root_path / clean_rel).resolve()
                    if not target_path.is_relative_to(root_path):
                        value["persistenceError"] = (
                            f"Security error: path {rel_path!r} escapes artifact_root_dir {root_path}"
                        )
                    else:
                        target_path.parent.mkdir(parents=True, exist_ok=True)
                        mode = str(block.props.get("mode", "write")).lower()
                        if mode == "append" and target_path.exists():
                            with target_path.open("a", encoding="utf-8") as f:
                                f.write(block.text)
                            if (
                                self.catalog is not None
                                and schema
                                and not self.catalog.should_emit_text(schema)
                            ):
                                value["text"] = target_path.read_text(encoding="utf-8")
                        else:
                            target_path.write_text(block.text, encoding="utf-8")
                        value["savedPath"] = str(target_path)
                        value["relativePath"] = clean_rel
                        value["bytes"] = target_path.stat().st_size
                except Exception as exc:
                    value["persistenceError"] = (
                        f"Failed to save artifact: {type(exc).__name__}: {exc}"
                    )

    # ── the tool path ─────────────────────────────────────────────────────

    def claims_item(self, item: Any) -> bool:
        return isinstance(item, UIItem)

    async def stage_item(
        self, item: Any, run: RunScope, *, convert: ChunkConverter
    ) -> AsyncIterator[BaseEvent]:
        if not self.enabled:
            return
        if item.signal is UISignal.BLOCK_START:
            value: dict[str, Any] = {
                "blockId": item.block_id,
                "index": self._next_index(run),
                "schema": item.schema,
                "props": dict(item.props),
                "body": self._body_mode(item.schema),
                "source": "tool",
            }
            item_schema = (
                self.catalog.default_item_schema(item.schema)
                if self.catalog is not None and item.schema
                else None
            )
            if item_schema:
                value["itemSchema"] = item_schema
            self._validate_props(value)
            self._open_blocks(run).append(item.block_id)
            tool_block = StreamUIBlock(
                block_id=item.block_id,
                index=value["index"],
                schema=item.schema,
                props=dict(item.props),
                body=value["body"],
            )
            self.data(run).setdefault("blocks", {})[item.block_id] = tool_block
            yield CustomEvent(type=EventType.CUSTOM, name=EVENT_BLOCK_START, value=value)
            return

        if item.signal is UISignal.ITEM:
            counts: dict[str, int] = self.data(run).setdefault("counts", {})
            index = counts.get(item.block_id, 0)
            counts[item.block_id] = index + 1
            value = {
                "blockId": item.block_id,
                "index": index,
                "schema": item.schema,
                "data": item.data,
            }
            await self._finish_item(value)
            yield CustomEvent(type=EventType.CUSTOM, name=EVENT_ITEM, value=value)
            return

        if item.signal is UISignal.TEXT:
            b = self.data(run).get("blocks", {}).get(item.block_id)
            if b is not None:
                b.text += item.delta
            yield CustomEvent(
                type=EventType.CUSTOM,
                name=EVENT_TEXT,
                value={"blockId": item.block_id, "delta": item.delta},
            )
            return

        value = {"blockId": item.block_id, "total": item.total, "truncated": False}
        await self._finish_block(value, item.block_id, run)
        self._close_block(run, item.block_id)
        yield CustomEvent(
            type=EventType.CUSTOM,
            name=EVENT_BLOCK_END,
            value=value,
        )

    # ── bracket bookkeeping ───────────────────────────────────────────────

    def _open_blocks(self, run: RunScope) -> list[str]:
        blocks: list[str] = self.data(run).setdefault("open", [])
        return blocks

    def _close_block(self, run: RunScope, block_id: str) -> None:
        blocks = self._open_blocks(run)
        if block_id in blocks:
            blocks.remove(block_id)

    def _next_index(self, run: RunScope) -> int:
        data = self.data(run)
        index = int(data.get("index", 0))
        data["index"] = index + 1
        return index

    def _body_mode(self, schema: str | None) -> str:
        if self.catalog is None or not schema:
            return "items"
        return self.catalog.body_mode(schema) or "items"

    async def on_run_finish(self, run: RunScope, *, error: bool) -> AsyncIterator[BaseEvent]:
        """Close any block still open when the run ended.

        A fence block is opened by text arriving and closed by more text
        arriving; a tool block is closed by the tool returning. If the run dies
        in between, nothing else will ever close it and the client is left with
        a card spinning forever. The sequencer cannot help — it does not know
        what a ``CUSTOM`` bracket is.

        The parsers are flushed first, and emptied, because this hook runs
        *before* Agno closes the message the text was arriving in. Sweeping
        without flushing would close the block here and then close it a second
        time when that ``TEXT_MESSAGE_END`` finally reached the stage — two
        ends for one block, the later one contradicting the earlier.
        """
        for message_id, parser in list(self.data(run).get("parsers", {}).items()):
            visible, produced = parser.flush()
            async for out in self._convert(produced, run, message_id):
                yield out
            if visible:
                yield TextMessageContentEvent(
                    type=EventType.TEXT_MESSAGE_CONTENT, message_id=message_id, delta=visible
                )
        self.data(run)["parsers"] = {}

        for block_id in list(self._open_blocks(run)):
            value: dict[str, Any] = {"blockId": block_id, "truncated": True}
            await self._finish_block(value, block_id, run)
            yield CustomEvent(
                type=EventType.CUSTOM,
                name=EVENT_BLOCK_END,
                value=value,
            )
        self.data(run)["open"] = []


def _compact(data: Any) -> str:
    try:
        return json.dumps(data, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(data)


__all__ = [
    "NAMESPACE",
    "StreamUIModule",
    "UIItem",
    "UISignal",
    "emit_item",
    "emit_text",
    "ui_block",
]
