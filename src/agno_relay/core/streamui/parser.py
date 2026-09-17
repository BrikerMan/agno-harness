"""The ``stream-ui`` fence — rich cards carried inside the text stream.

The agent writes a fenced block into its ordinary answer::

    Here is what I found:

    ```stream-ui {"schema": "movie-list", "title": "科幻推荐"}
    {"id": 123}
    {"id": 456}
    ```

    And here is why I picked them.

The fence header is a JSON object: ``schema`` says which card, everything else
is that block's properties. There is deliberately no second, shorter header
syntax — two syntaxes are two parsers and two paragraphs of prompt, to save the
model typing braces it already types on every body line.

Three details are less obvious than they look.

*Fence length follows CommonMark.* Open with N backticks (N≥3) and the block
closes on the first line with N or more. A card that displays markdown, or code
containing a fence, opens with four. Treating every ``` as a closer would cut
those blocks in half; the variable length is the standard escape and the model
already knows it.

*Streaming.* Deltas arrive at arbitrary byte boundaries, so the fence marker,
the header JSON and every body line can be split anywhere. The parser holds
back any tail that could still turn out to be a marker and releases only text
it is sure about. Without that hold-back, raw JSON flashes on screen for a frame
before the client catches up — exactly the ugliness this exists to prevent.

*The fence is its own bracket.* There is no explicit block-end line, because a
closing marker the model must remember to write is a closing marker the model
will sometimes forget, and ``` already closes. If the stream ends inside a
fence the block is closed anyway and marked ``truncated``.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from .schema import BodyMode

FENCE_TAG = "stream-ui"

EVENT_BLOCK_START = "ui.block.start"
EVENT_ITEM = "ui.item"
EVENT_TEXT = "ui.text"
EVENT_BLOCK_END = "ui.block.end"

_OPEN_RE = re.compile(r"^(`{3,})[ \t]*" + re.escape(FENCE_TAG) + r"[ \t]*([^\n]*)$")
_OPEN_MD_RE = _OPEN_RE
_OPEN_XML_RE = re.compile(
    r"^[ \t]*<" + re.escape(FENCE_TAG) + r"(?P<attr>[^>]*)>(?P<after>.*)$",
    re.IGNORECASE,
)
_CLOSE_XML_RE = re.compile(r"</[ \t]*" + re.escape(FENCE_TAG) + r"[ \t]*>", re.IGNORECASE)
_ATTR_RE = re.compile(r'([a-zA-Z0-9_\-]+)\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|([^\s>]+))')


def _xml_close_candidate_len(text: str) -> int:
    """If the tail of text could grow into a closing XML tag (e.g. '</stream-ui>'),
    return the length of that tail so it is held back in the buffer.
    Otherwise return 0.
    """
    last_lt = text.rfind("<")
    if last_lt < 0:
        return 0
    candidate = text[last_lt:].lower()
    norm = re.sub(r"\s+", "", candidate)
    full_target = f"</{FENCE_TAG}>"
    if full_target.startswith(norm):
        return len(text) - last_lt
    return 0


def _parse_header(header: str) -> tuple[str | None, dict[str, Any], str | None]:
    if not header:
        return None, {}, 'the fence header must be a JSON object with a "schema" key'
    try:
        parsed = json.loads(header)
    except json.JSONDecodeError as exc:
        return None, {}, f"invalid header JSON: {exc.msg}"
    if not isinstance(parsed, dict):
        return None, {}, "the fence header must be a JSON object"
    schema = parsed.pop("schema", None)
    if not isinstance(schema, str) or not schema:
        return None, parsed, 'the fence header needs a "schema" string'
    return schema, parsed, None


def _parse_xml_opener(
    line: str,
) -> tuple[bool, str | None, dict[str, Any], str | None] | None:
    """Check if line is an opening XML fence.

    Returns None if not an XML opener.
    Returns (awaiting_header, schema, props, error).
    If awaiting_header is True, schema/props/error are None.
    """
    m = _OPEN_XML_RE.match(line)
    if not m:
        return None
    attr = m.group("attr").strip()
    after = m.group("after").strip()

    # Case 2: JSON inside tag: <stream-ui {"schema": ...}>
    if attr.startswith("{") and attr.endswith("}"):
        schema, props, err = _parse_header(attr)
        return False, schema, props, err

    # Case 3: JSON right after tag: <stream-ui>{"schema": ...}
    if after.startswith("{"):
        schema, props, err = _parse_header(after)
        return False, schema, props, err

    # Case 4: XML attributes: <stream-ui schema="artifact" title="...">
    if attr:
        attrs = _ATTR_RE.findall(attr)
        if attrs:
            parsed_props: dict[str, Any] = {}
            for k, v_double, v_single, v_raw in attrs:
                raw_val = v_double or v_single or v_raw
                if raw_val.lower() == "true":
                    val: Any = True
                elif raw_val.lower() == "false":
                    val = False
                elif raw_val.lower() == "null":
                    val = None
                elif raw_val.isdigit():
                    val = int(raw_val)
                elif raw_val.startswith(("{", "[")):
                    try:
                        val = json.loads(raw_val)
                    except json.JSONDecodeError:
                        val = raw_val
                else:
                    val = raw_val
                parsed_props[k] = val
            schema = parsed_props.pop("schema", None)
            if not schema:
                return False, None, parsed_props, 'the fence header needs a "schema" attribute'
            return False, schema, parsed_props, None

    # Case 1: Scheme A (multiline) - header on next line!
    return True, None, {}, None


class Catalog(Protocol):
    """What the parser needs from a catalog: how to read a body, and what the
    lines are called when they do not say so themselves."""

    allow_unknown: bool

    def knows_block(self, name: str) -> bool: ...
    def body_mode(self, name: str) -> BodyMode | None: ...
    def default_item_schema(self, block_name: str) -> str | None: ...
    def should_emit_text(self, block_name: str) -> bool: ...
    def parse_line(self, block_name: str, line: str, block: Any) -> Mapping[str, Any] | None: ...


class _Mode(StrEnum):
    OUTSIDE = "outside"
    INSIDE = "inside"


@dataclass
class StreamUIEvent:
    """Transport-neutral parser output, wrapped as ``CUSTOM`` events later."""

    kind: str
    block_id: str
    payload: dict[str, Any]


@dataclass
class StreamUIItem:
    """One body line of an ``items`` block. ``data`` or ``error``, never both."""

    index: int
    raw: str
    schema: str | None = None
    data: Any = None
    error: str | None = None

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"index": self.index}
        if self.schema:
            payload["schema"] = self.schema
        if self.error is None:
            payload["data"] = self.data
        else:
            payload["raw"] = self.raw
            payload["error"] = self.error
        return payload


@dataclass
class StreamUIBlock:
    """One fenced block, as it is being built up."""

    block_id: str
    index: int
    schema: str | None = None
    props: dict[str, Any] = field(default_factory=dict)
    body: BodyMode = "items"
    items: list[StreamUIItem] = field(default_factory=list)
    text: str = ""
    error: str | None = None
    truncated: bool = False


class StreamUIFenceParser:
    """Incremental extractor for ``stream-ui`` fences.

    Feed text — a whole answer or a single streaming delta — to :meth:`feed`; it
    returns the text that is safe to show plus the block events that input
    completed. Call :meth:`flush` once at the end to release held-back text and
    close a fence the model left open.
    """

    def __init__(self, catalog: Catalog | None = None) -> None:
        self._catalog = catalog
        self._buffer = ""
        self._mode = _Mode.OUTSIDE
        self._at_line_start = True
        self._fence_len = 3
        self._fence_kind: str = "markdown"
        self._awaiting_header: bool = False
        self._header_buffer: str = ""
        self._pending_open_line: str = ""
        self._text_line_buffer: str = ""
        self._block: StreamUIBlock | None = None
        self._block_count = 0
        self.blocks: list[StreamUIBlock] = []
        self._blocks_by_id: dict[str, StreamUIBlock] = {}

    def get_block(self, block_id: str) -> StreamUIBlock | None:
        """Look up a block parsed by this parser by its block_id."""
        return self._blocks_by_id.get(block_id)

    # ── public API ────────────────────────────────────────────────────────

    def feed(self, delta: str) -> tuple[str, list[StreamUIEvent]]:
        if not delta:
            return "", []
        self._buffer += delta
        visible: list[str] = []
        events: list[StreamUIEvent] = []
        for text, produced in self._drain():
            if text:
                visible.append(text)
            events.extend(produced)
        return "".join(visible), events

    def flush(self) -> tuple[str, list[StreamUIEvent]]:
        """Release whatever is still held back. Safe to call more than once."""
        visible = ""
        events: list[StreamUIEvent] = []
        remainder, self._buffer = self._buffer, ""

        if self._mode is _Mode.INSIDE:
            closed = False
            if self._awaiting_header:
                self._awaiting_header = False
                header_line = (self._header_buffer + remainder).strip()
                if header_line:
                    schema, props, err = _parse_header(header_line)
                else:
                    schema, props, err = (
                        None,
                        {},
                        'the fence header must be a JSON object with a "schema" key',
                    )
                events.append(
                    self._create_block_with_header(
                        self._pending_open_line, schema=schema, props=props, error=err
                    )
                )
                events.append(self._end_block(truncated=True))
            elif self._fence_kind == "xml":
                match = _CLOSE_XML_RE.search(remainder)
                if match is not None:
                    closed = True
                    body = remainder[: match.start()]
                    visible = remainder[match.end() :]
                else:
                    body = remainder
                if body:
                    if self._block is not None and self._block.body == "text":
                        events.extend(self._feed_text_for_parsing(body, flush=True))
                        ev = self._append_text(body)
                        if ev is not None:
                            events.append(ev)
                    elif self._block is not None and self._block.body == "items":
                        for line in body.splitlines():
                            if line.strip():
                                events.append(self._consume_item(line))
                else:
                    if self._block is not None and self._block.body == "text":
                        events.extend(self._feed_text_for_parsing("", flush=True))
                events.append(self._end_block(truncated=not closed))
            else:
                if remainder:
                    if self._is_close(remainder):
                        closed = True
                    elif self._block is not None and self._block.body == "text":
                        events.extend(self._feed_text_for_parsing(remainder, flush=True))
                        ev = self._append_text(remainder)
                        if ev is not None:
                            events.append(ev)
                    else:
                        events.append(self._consume_item(remainder))
                else:
                    if self._block is not None and self._block.body == "text":
                        events.extend(self._feed_text_for_parsing("", flush=True))
                events.append(self._end_block(truncated=not closed))
        elif remainder:
            # A half-written opener at the very end means the model was cut off
            # mid-fence. Showing "```stream-" to the user helps nobody, so the
            # fragment is dropped and the truncation is reported as an empty
            # block instead.
            if self._at_line_start:
                if _OPEN_MD_RE.match(remainder):
                    events.append(self._start_block(remainder))
                    events.append(self._end_block(truncated=True))
                elif _OPEN_XML_RE.match(remainder):
                    xml_open = _parse_xml_opener(remainder)
                    if xml_open is not None:
                        _, schema, props, err = xml_open
                        events.append(
                            self._create_block_with_header(
                                remainder, schema=schema, props=props, error=err
                            )
                        )
                        events.append(self._end_block(truncated=True))
                    else:
                        visible = remainder
                else:
                    visible = remainder
            else:
                visible = remainder
        self._at_line_start = True
        return visible, events

    @property
    def in_fence(self) -> bool:
        return self._mode is _Mode.INSIDE

    # ── state machine ─────────────────────────────────────────────────────

    def _drain(self) -> Iterator[tuple[str, list[StreamUIEvent]]]:
        while True:
            step = self._drain_outside() if self._mode is _Mode.OUTSIDE else self._drain_inside()
            if step is None:
                return
            yield step

    def _drain_outside(self) -> tuple[str, list[StreamUIEvent]] | None:
        for start, line, end in self._complete_lines():
            xml_open = _parse_xml_opener(line)
            if xml_open is not None:
                awaiting_header, schema, props, err = xml_open
                before = self._buffer[:start]
                self._buffer = self._buffer[end:]
                self._at_line_start = True
                self._mode = _Mode.INSIDE
                self._fence_kind = "xml"
                if awaiting_header:
                    self._awaiting_header = True
                    self._pending_open_line = line
                    return before, []
                else:
                    self._awaiting_header = False
                    ev = self._create_block_with_header(line, schema=schema, props=props, error=err)
                    return before, [ev]

            if _OPEN_MD_RE.match(line):
                before = self._buffer[:start]
                self._buffer = self._buffer[end:]
                self._at_line_start = True
                self._mode = _Mode.INSIDE
                self._fence_kind = "markdown"
                self._awaiting_header = False
                return before, [self._start_block(line)]

        # No opener in what has arrived. Release everything except a tail that
        # could still grow into one.
        head, tail = self._split_tail()
        if tail and not _could_open(tail, at_line_start=self._at_line_start or bool(head)):
            head, tail = head + tail, ""
        if not head:
            return None
        self._buffer = tail
        self._at_line_start = head.endswith("\n")
        return head, []

    def _drain_inside(self) -> tuple[str, list[StreamUIEvent]] | None:
        if self._awaiting_header:
            while "\n" in self._buffer:
                newline_at = self._buffer.find("\n")
                line = self._buffer[:newline_at].rstrip("\r")
                end = newline_at + 1
                if not line.strip() and not self._header_buffer:
                    self._buffer = self._buffer[end:]
                    continue
                if _CLOSE_XML_RE.search(line):
                    self._awaiting_header = False
                    self._buffer = self._buffer[end:]
                    start_ev = self._create_block_with_header(
                        self._pending_open_line,
                        schema=None,
                        props={},
                        error='the fence header must be a JSON object with a "schema" key',
                    )
                    end_ev = self._end_block()
                    return "", [start_ev, end_ev]

                self._buffer = self._buffer[end:]
                self._header_buffer += line + "\n"
                stripped = self._header_buffer.strip()
                if not stripped.startswith("{"):
                    self._awaiting_header = False
                    schema, props, err = _parse_header(stripped)
                    ev = self._create_block_with_header(
                        self._pending_open_line, schema=schema, props=props, error=err
                    )
                    return "", [ev]

                try:
                    json.loads(stripped)
                except json.JSONDecodeError:
                    if self._header_buffer.count("\n") > 30:
                        self._awaiting_header = False
                        schema, props, err = _parse_header(stripped)
                        ev = self._create_block_with_header(
                            self._pending_open_line, schema=schema, props=props, error=err
                        )
                        return "", [ev]
                    continue
                else:
                    self._awaiting_header = False
                    self._header_buffer = ""
                    schema, props, err = _parse_header(stripped)
                    ev = self._create_block_with_header(
                        self._pending_open_line, schema=schema, props=props, error=err
                    )
                    return "", [ev]
            return None

        if self._fence_kind == "xml":
            return self._drain_inside_xml()
        return self._drain_inside_markdown()

    def _drain_inside_xml(self) -> tuple[str, list[StreamUIEvent]] | None:
        block = self._block
        assert block is not None

        events: list[StreamUIEvent] = []
        match = _CLOSE_XML_RE.search(self._buffer)
        if match is not None:
            after = self._buffer[match.end() :]
            # If the closing tag is at the very end of the buffer, hold it back to see
            # if a trailing newline follows in the next chunk, unless flush is called.
            if match.end() == len(self._buffer) or after == "\r":
                body = self._buffer[: match.start()]
                self._buffer = self._buffer[match.start() :]
                if not body:
                    return None
                if block.body == "text":
                    events.extend(self._feed_text_for_parsing(body))
                    ev = self._append_text(body)
                    if ev is not None:
                        events.append(ev)
                elif block.body == "items":
                    for line in body.splitlines():
                        if line.strip():
                            events.append(self._consume_item(line))
                return "", events

            body = self._buffer[: match.start()]
            if body:
                if block.body == "text":
                    events.extend(self._feed_text_for_parsing(body, flush=True))
                    ev = self._append_text(body)
                    if ev is not None:
                        events.append(ev)
                elif block.body == "items":
                    for line in body.splitlines():
                        if line.strip():
                            events.append(self._consume_item(line))
            else:
                if block.body == "text":
                    events.extend(self._feed_text_for_parsing("", flush=True))
            if after.startswith("\r\n"):
                after = after[2:]
            elif after.startswith("\n"):
                after = after[1:]
            self._buffer = after
            self._at_line_start = True
            events.append(self._end_block())
            return "", events

        if block.body == "items":
            for _start, line, end in self._complete_lines():
                self._buffer = self._buffer[end:]
                return "", [] if not line.strip() else [self._consume_item(line)]
            return None

        # Text mode: emit text as it arrives, holding back only any suffix that could be a prefix of </stream-ui>
        hold_len = _xml_close_candidate_len(self._buffer)
        if hold_len > 0:
            head = self._buffer[:-hold_len]
            tail = self._buffer[-hold_len:]
        else:
            head = self._buffer
            tail = ""

        if not head:
            return None

        self._buffer = tail
        self._at_line_start = head.endswith("\n")
        events.extend(self._feed_text_for_parsing(head))
        ev = self._append_text(head)
        if ev is not None:
            events.append(ev)
        return "", events

    def _drain_inside_markdown(self) -> tuple[str, list[StreamUIEvent]] | None:
        block = self._block
        assert block is not None  # only reachable inside a fence

        events: list[StreamUIEvent] = []
        for start, line, end in self._complete_lines():
            if self._is_close(line):
                body = self._buffer[:start]
                if body and block.body == "text":
                    events.extend(self._feed_text_for_parsing(body, flush=True))
                    ev = self._append_text(body)
                    if ev is not None:
                        events.append(ev)
                elif block.body == "text":
                    events.extend(self._feed_text_for_parsing("", flush=True))
                self._buffer = self._buffer[end:]
                self._at_line_start = True
                events.append(self._end_block())
                return "", events
            if block.body == "items":
                self._buffer = self._buffer[end:]
                return "", [] if not line.strip() else [self._consume_item(line)]

        if block.body == "items":
            return None

        # Text body: release as it arrives so a long code card renders while it
        # is still being written, holding back only a possible closing fence.
        head, tail = self._split_tail()
        if tail and not _could_close(tail):
            head, tail = head + tail, ""
        if not head:
            return None
        self._buffer = tail
        self._at_line_start = head.endswith("\n")
        events.extend(self._feed_text_for_parsing(head))
        ev = self._append_text(head)
        if ev is not None:
            events.append(ev)
        return "", events

    def _complete_lines(self) -> Iterator[tuple[int, str, int]]:
        """Yield ``(start, line, end)`` for each newline-terminated line.

        ``start`` is the line's first character and ``end`` the index just past
        its newline, so callers can slice around a line without re-scanning.
        """
        pos = 0
        if not self._at_line_start:
            first = self._buffer.find("\n")
            if first < 0:
                return
            pos = first + 1
        while True:
            newline_at = self._buffer.find("\n", pos)
            if newline_at < 0:
                return
            yield pos, self._buffer[pos:newline_at].rstrip("\r"), newline_at + 1
            pos = newline_at + 1

    def _split_tail(self) -> tuple[str, str]:
        last_newline = self._buffer.rfind("\n")
        if last_newline < 0:
            return "", self._buffer
        return self._buffer[: last_newline + 1], self._buffer[last_newline + 1 :]

    def _is_close(self, line: str) -> bool:
        stripped = line.strip()
        return bool(stripped) and set(stripped) == {"`"} and len(stripped) >= self._fence_len

    # ── block bookkeeping ─────────────────────────────────────────────────

    def _create_block_with_header(
        self,
        opening_line: str,
        *,
        schema: str | None = None,
        props: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> StreamUIEvent:
        self._block_count += 1
        block = StreamUIBlock(block_id=f"ui-{uuid.uuid4().hex[:12]}", index=self._block_count - 1)
        self._block = block
        self.blocks.append(block)
        self._blocks_by_id[block.block_id] = block

        block.schema = schema
        block.props = props or {}
        block.error = error

        if block.error is None and block.schema is not None:
            block.body = self._body_mode(block.schema, block)
        else:
            # Keep the body verbatim so a header the model got wrong shows up as
            # a visible, fixable error rather than a card that silently vanished.
            block.body = "text"

        payload: dict[str, Any] = {
            "blockId": block.block_id,
            "index": block.index,
            "schema": block.schema,
            "props": block.props,
            "body": block.body,
        }
        item_schema = self._default_item_schema(block.schema)
        if item_schema:
            payload["itemSchema"] = item_schema
        if block.error:
            payload["error"] = block.error
            payload["raw"] = opening_line
        return StreamUIEvent(kind=EVENT_BLOCK_START, block_id=block.block_id, payload=payload)

    def _start_block(self, opening_line: str) -> StreamUIEvent:
        match = _OPEN_RE.match(opening_line)
        self._fence_len = len(match.group(1)) if match else 3
        header = (match.group(2) if match else "").strip()
        schema, props, error = _parse_header(header)
        return self._create_block_with_header(opening_line, schema=schema, props=props, error=error)

    def _body_mode(self, schema: str, block: StreamUIBlock) -> BodyMode:
        if self._catalog is None:
            return "items"
        mode = self._catalog.body_mode(schema)
        if mode is not None:
            return mode
        if self._catalog.allow_unknown:
            return "items"
        block.error = f"unknown block schema {schema!r}"
        return "text"

    @property
    def _unvalidated(self) -> bool:
        return self._catalog is None or self._catalog.allow_unknown

    def _default_item_schema(self, schema: str | None) -> str | None:
        if schema is None or self._catalog is None:
            return None
        return self._catalog.default_item_schema(schema)

    def _consume_item(self, raw: str) -> StreamUIEvent:
        block = self._block
        assert block is not None
        index = len(block.items)
        default_schema = self._default_item_schema(block.schema)
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            # A bad line must not kill the block: the rest may be perfectly
            # good, and the client can show the failure in place.
            item = StreamUIItem(index=index, raw=raw, error=f"invalid JSON: {exc.msg}")
        else:
            schema = default_schema
            if isinstance(parsed, dict) and isinstance(parsed.get("schema"), str):
                parsed = dict(parsed)
                schema = parsed.pop("schema")
            if schema is None and self._unvalidated:
                # Nothing knows what this block's lines are called, and nothing
                # is going to check: keep the block's own name so the frontend
                # still has something to dispatch on.
                schema = block.schema
            if schema is None:
                item = StreamUIItem(
                    index=index,
                    raw=raw,
                    data=parsed,
                    error='item needs a "schema" key: this block is not homogeneous',
                )
            else:
                item = StreamUIItem(index=index, raw=raw, schema=schema, data=parsed)
        block.items.append(item)
        return StreamUIEvent(
            kind=EVENT_ITEM,
            block_id=block.block_id,
            payload={"blockId": block.block_id, **item.to_payload()},
        )

    def _should_emit_text(self, schema: str | None) -> bool:
        if schema is None or self._catalog is None:
            return True
        if hasattr(self._catalog, "should_emit_text"):
            return self._catalog.should_emit_text(schema)
        return True

    def _feed_text_for_parsing(self, text: str, *, flush: bool = False) -> list[StreamUIEvent]:
        block = self._block
        if block is None or not block.schema or self._catalog is None:
            return []
        if not hasattr(self._catalog, "parse_line"):
            return []
        events: list[StreamUIEvent] = []
        if text:
            self._text_line_buffer += text
        while "\n" in self._text_line_buffer:
            line, self._text_line_buffer = self._text_line_buffer.split("\n", 1)
            clean = line.strip()
            if not clean:
                continue
            res = self._catalog.parse_line(block.schema, clean, block)
            if res is not None:
                events.append(self._consume_extracted_item(res, line))
        if flush and self._text_line_buffer.strip():
            clean = self._text_line_buffer.strip()
            res = self._catalog.parse_line(block.schema, clean, block)
            if res is not None:
                events.append(self._consume_extracted_item(res, self._text_line_buffer))
            self._text_line_buffer = ""
        return events

    def _parse_lines_in_text(self, text: str) -> list[StreamUIEvent]:
        return self._feed_text_for_parsing(text)

    def _consume_extracted_item(self, data: Mapping[str, Any], raw: str) -> StreamUIEvent:
        block = self._block
        assert block is not None
        index = len(block.items)
        schema = (
            data.get("schema")
            if isinstance(data.get("schema"), str)
            else self._default_item_schema(block.schema)
        )
        if schema is None and self._unvalidated:
            schema = block.schema
        data_dict = dict(data)
        data_dict.pop("schema", None)
        item = StreamUIItem(index=index, raw=raw, schema=schema, data=data_dict)
        block.items.append(item)
        return StreamUIEvent(
            kind=EVENT_ITEM,
            block_id=block.block_id,
            payload={"blockId": block.block_id, **item.to_payload()},
        )

    def _append_text(self, delta: str) -> StreamUIEvent | None:
        block = self._block
        assert block is not None
        block.text += delta
        if self._should_emit_text(block.schema):
            return StreamUIEvent(
                kind=EVENT_TEXT,
                block_id=block.block_id,
                payload={"blockId": block.block_id, "delta": delta},
            )
        return None

    def _end_block(self, *, truncated: bool = False) -> StreamUIEvent:
        block = self._block
        assert block is not None
        self._block = None
        self._mode = _Mode.OUTSIDE
        self._fence_kind = "markdown"
        self._awaiting_header = False
        self._header_buffer = ""
        self._pending_open_line = ""
        self._fence_len = 3
        self._text_line_buffer = ""
        block.truncated = truncated or block.truncated
        payload: dict[str, Any] = {
            "blockId": block.block_id,
            "total": len(block.items),
            "truncated": block.truncated,
        }
        return StreamUIEvent(kind=EVENT_BLOCK_END, block_id=block.block_id, payload=payload)


def _could_open(tail: str, *, at_line_start: bool) -> bool:
    """Could this partial line still become an opening fence?"""
    if not at_line_start:
        return False
    cleaned = tail.lstrip(" \t")
    if cleaned.startswith("<"):
        target = "<" + FENCE_TAG
        norm = re.sub(r"\s+", "", cleaned.lower())
        if target.startswith(norm) or norm.startswith(target):
            return True
    if tail.startswith("`"):
        ticks = len(tail) - len(tail.lstrip("`"))
        if ticks < 3:
            return tail == "`" * ticks
        rest = tail[ticks:].lstrip(" \t")
        return FENCE_TAG.startswith(rest) or rest.startswith(FENCE_TAG)
    return False


def _could_close(tail: str) -> bool:
    """Could this partial line still become a closing fence?"""
    return bool(tail) and set(tail) == {"`"}


def parse_streamui_text(
    text: str, catalog: Catalog | None = None
) -> tuple[str, list[StreamUIBlock]]:
    """One-shot parse of a complete answer.

    History runs through the same state machine as live streaming, one big
    delta instead of many small ones, so a replayed message cannot disagree with
    what the user originally saw.
    """
    parser = StreamUIFenceParser(catalog)
    visible, _ = parser.feed(text)
    tail, _ = parser.flush()
    return visible + tail, parser.blocks


def blocks_to_payload(blocks: list[StreamUIBlock]) -> list[dict[str, Any]]:
    """Serialize blocks for a replayed message.

    Note what is missing: ``resolved``. A resolver is an ``await`` against a live
    data source and this is a pure re-parse of stored text, so a replayed card
    carries the identifiers the model wrote rather than the facts the server
    added. Replaying the recorded frames instead of re-deriving them is what
    closes that gap.
    """
    return [
        {
            "blockId": block.block_id,
            "index": block.index,
            "schema": block.schema,
            "props": block.props,
            "body": block.body,
            "truncated": block.truncated,
            "error": block.error,
            "items": [item.to_payload() for item in block.items],
            "text": block.text,
        }
        for block in blocks
    ]


__all__ = [
    "EVENT_BLOCK_END",
    "EVENT_BLOCK_START",
    "EVENT_ITEM",
    "EVENT_TEXT",
    "FENCE_TAG",
    "Catalog",
    "StreamUIBlock",
    "StreamUIEvent",
    "StreamUIFenceParser",
    "StreamUIItem",
    "blocks_to_payload",
    "parse_streamui_text",
]
