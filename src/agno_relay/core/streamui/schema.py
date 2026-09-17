"""Card schemas and the catalog that owns them.

One declaration, four consumers. A schema written here becomes the prompt the
model reads, the validation the server applies, the JSON Schema the frontend
generates types from, and the lookup the parser uses to decide how to read a
block's body. Before this, the card shapes lived in a hand-written prompt and
nothing kept that prompt, the server and the renderer in agreement.

TWO IDEAS DO MOST OF THE WORK
-----------------------------
*Closed vocabularies.* Any field that decides how something is drawn — which
icon, which animation, which colour — is a ``Literal``. The model picks a
semantic token and the frontend maps it to an asset. Changing the theme then
costs nothing, because the prompt never mentioned the assets.

*Resolvers.* For data-heavy cards the model supplies an identifier and the
server supplies the facts. The model is good at choosing and ordering and bad at
transcribing a rating without changing it, so it does the first and not the
second. A poster URL cannot be hallucinated if the model never writes one.
"""

from __future__ import annotations

import contextlib
import inspect
import json
from collections.abc import Awaitable, Callable, Iterable, Mapping
from typing import Annotated, Any, ClassVar, Literal, cast, get_args, get_origin
from urllib.parse import urlparse

from pydantic import AfterValidator, BaseModel, ConfigDict, ValidationError, ValidationInfo

BodyMode = Literal["items", "text"]

Resolver = Callable[[Any], Awaitable[Mapping[str, Any] | None]]
OnCompleteHandler = Callable[
    [Any, Any], Awaitable[Mapping[str, Any] | None] | Mapping[str, Any] | None
]


def _validate_media_url(value: str, info: ValidationInfo) -> str:
    """Reject anything that is not an https URL on an allowed host.

    With resolvers in place this is a backstop rather than the main defence —
    most media comes from the server. It covers the residue: a card where the
    model genuinely does supply the link.
    """
    parsed = urlparse(value)
    if parsed.scheme != "https":
        raise ValueError("media URLs must use https")
    host = parsed.hostname
    if not host:
        raise ValueError("media URLs must have a host")
    allowed = (info.context or {}).get("allowed_media_domains") or ()
    if allowed and not _host_allowed(host, allowed):
        raise ValueError(f"host {host!r} is not in the allowed media domains")
    return value


def _host_allowed(host: str, allowed: Iterable[str]) -> bool:
    host = host.lower()
    for domain in allowed:
        domain = domain.lower().lstrip(".")
        if host == domain or host.endswith(f".{domain}"):
            return True
    return False


#: An https URL, optionally restricted to the catalog's allowed domains.
MediaUrl = Annotated[str, AfterValidator(_validate_media_url)]


class _EmptyProps(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BlockSchema(BaseModel):
    """One kind of card block. Fields are the fence header's properties.

    ``body`` decides how the parser reads what is between the fences:

    ``items``
        One JSON object per line, rendered as it arrives. Lists, tables, steps,
        citations, timelines.
    ``text``
        Raw text, never parsed as JSON. Code, diffs, long markdown.

    ``text`` is not a convenience. Putting a hundred lines of code inside a JSON
    string means nothing renders until the last character arrives, every quote
    and backslash has to be escaped correctly by a model that frequently does
    not, and one bad escape destroys the entire card rather than one line of it.
    A raw body has none of those problems.
    """

    model_config = ConfigDict(extra="forbid")

    schema_name: ClassVar[str] = ""
    body: ClassVar[BodyMode] = "items"

    #: Whether raw text chunks are emitted as ui.text events when body == "text".
    #: Set to False when a block (such as an HTML presentation deck) accumulates
    #: text internally for workspace file writing without flooding the UI stream with raw code.
    emit_text: ClassVar[bool] = True

    #: Whether this schema should be included in the system prompt instructions.
    #: Set to False for tool-driven cards (like todo-list, diff, research-graph)
    #: or skill-specific cards that are injected on-demand.
    include_in_system_prompt: ClassVar[bool] = True

    #: Item type for a homogeneous block, letting its lines omit ``schema``.
    item: ClassVar[type[BaseModel] | None] = None

    @classmethod
    def props_model(cls) -> type[BaseModel]:
        return cls

    @classmethod
    def item_model(cls) -> type[BaseModel] | None:
        return cls.item

    @classmethod
    def should_emit_text(cls) -> bool:
        return cls.emit_text

    @classmethod
    def should_include_in_system_prompt(cls) -> bool:
        return cls.include_in_system_prompt

    @classmethod
    def parse_line(cls, line: str, block: Any) -> Mapping[str, Any] | None:
        """Hook called per completed line in a text body.

        If it returns a mapping, the parser emits it as a ui.item event.
        """
        return None

    @classmethod
    async def on_complete(cls, block: Any, scope: Any = None) -> Mapping[str, Any] | None:
        """Hook called when the block closes (e.g. for deterministic persistence).

        Any returned mapping is merged into the ui.block.end event payload.
        """
        return None

    def render_teams_block(self, rendered_items: list[dict[str, Any]]) -> dict[str, Any] | None:
        """Render self and child item fragments into a single Teams Adaptive Card.

        Subclasses can override this. If returning None, CardCatalog provides
        an automatic fallback Adaptive Card.
        """
        return None

    def render_lark_block(self, rendered_items: list[dict[str, Any]]) -> dict[str, Any] | None:
        """Render self and child item fragments into a Lark Interactive Card v2.

        Subclasses can override this. If returning None, CardCatalog provides
        an automatic fallback Lark Interactive Card.
        """
        return None

    def render_cli_block(self, rendered_items: list[str]) -> str | None:
        """Render self and child items for rich terminal display."""
        return None


class ItemSchema(BaseModel):
    """One line inside an ``items`` block."""

    model_config = ConfigDict(extra="forbid")

    schema_name: ClassVar[str] = ""

    async def resolve(self, ctx: Any = None) -> Mapping[str, Any] | None:
        """Class-First fact resolver.

        Subclasses override this to fetch authoritative facts asynchronously.
        Defaults to None.
        """
        return None

    def render_teams(self, resolved: Mapping[str, Any]) -> dict[str, Any] | None:
        """Render self as a fragment inside a Teams Adaptive Card."""
        return None

    def render_lark(self, resolved: Mapping[str, Any]) -> dict[str, Any] | None:
        """Render self as a fragment inside a Lark Interactive Card v2."""
        return None

    def render_cli(self, resolved: Mapping[str, Any]) -> str | None:
        """Render text/Rich fragment for CLI display."""
        return None


class CardSchema(BlockSchema):
    """A block that is exactly one item, declared once instead of twice.

    Weather, a hero stat, a single repository: the block carries no properties
    of its own and the body is one line. Written as a pair of schemas that would
    be two names in the prompt for one thing on screen.
    """

    @classmethod
    def props_model(cls) -> type[BaseModel]:
        return _EmptyProps

    @classmethod
    def item_model(cls) -> type[BaseModel]:
        return cls

    async def resolve(self, ctx: Any = None) -> Mapping[str, Any] | None:
        """Class-First fact resolver for single-card blocks."""
        return None

    def render_teams(self, resolved: Mapping[str, Any]) -> dict[str, Any] | None:
        """Render self for Teams."""
        return None

    def render_lark(self, resolved: Mapping[str, Any]) -> dict[str, Any] | None:
        """Render self for Lark."""
        return None

    def render_cli(self, resolved: Mapping[str, Any]) -> str | None:
        """Render self for CLI."""
        return None


class CatalogConflict(ValueError):
    """Two schemas registered under the same name."""


class CardCatalog:
    """The registry of card schemas, and the single source of truth about them.

    Parameters
    ----------
    schemas:
        Block schemas to register. A block's ``item`` type is registered
        alongside it, so declaring the block is enough.
    allow_unknown:
        Pass blocks whose schema is not registered straight through instead of
        turning them into visible error blocks. An escape hatch for prototyping;
        it means the frontend decides what an unknown card looks like.
    allowed_media_domains:
        Hosts that :data:`MediaUrl` fields may point at. Empty means any https
        host is accepted.
    """

    def __init__(
        self,
        schemas: Iterable[type[BlockSchema]] = (),
        *,
        allow_unknown: bool = False,
        allowed_media_domains: Iterable[str] = (),
    ) -> None:
        self.allow_unknown = allow_unknown
        self.allowed_media_domains = tuple(allowed_media_domains)
        self._blocks: dict[str, type[BlockSchema]] = {}
        self._items: dict[str, type[BaseModel]] = {}
        self._resolvers: dict[str, Resolver] = {}
        self._on_complete: dict[str, OnCompleteHandler] = {}
        for schema in schemas:
            self.register(schema)

    # ── registration ──────────────────────────────────────────────────────

    def register(self, schema: type[BlockSchema]) -> CardCatalog:
        name = schema.schema_name
        if not name:
            raise CatalogConflict(f"{schema.__name__} has no schema_name")
        existing = self._blocks.get(name)
        if existing is not None and existing is not schema:
            raise CatalogConflict(
                f"two block schemas are named {name!r}: {existing.__name__} and {schema.__name__}"
            )
        self._blocks[name] = schema

        item = schema.item_model()
        if item is not None and item is not schema:
            self.register_item(item)
        return self

    def register_item(self, item: type[BaseModel]) -> CardCatalog:
        name = getattr(item, "schema_name", "")
        if not name:
            raise CatalogConflict(f"{item.__name__} has no schema_name")
        existing = self._items.get(name)
        if existing is not None and existing is not item:
            raise CatalogConflict(
                f"two item schemas are named {name!r}: {existing.__name__} and {item.__name__}"
            )
        self._items[name] = item
        return self

    def __or__(self, other: CardCatalog) -> CardCatalog:
        """Merge two catalogs, rejecting name collisions.

        A skill is a catalog. Combining skills is combining catalogs, and two
        skills that both define ``chart`` must fail loudly at assembly rather
        than silently render one of them wrong.
        """
        merged = CardCatalog(
            allow_unknown=self.allow_unknown or other.allow_unknown,
            allowed_media_domains={*self.allowed_media_domains, *other.allowed_media_domains},
        )
        for source in (self, other):
            for schema in source._blocks.values():
                merged.register(schema)
            for item in source._items.values():
                merged.register_item(item)
            for name, resolver in source._resolvers.items():
                merged.set_resolver(name, resolver)
            for name, on_comp in source._on_complete.items():
                merged.set_on_complete(name, on_comp)
        return merged

    # ── lookup ────────────────────────────────────────────────────────────

    @property
    def block_names(self) -> list[str]:
        return sorted(self._blocks)

    @property
    def item_names(self) -> list[str]:
        return sorted(self._items)

    def knows_block(self, name: str) -> bool:
        return name in self._blocks

    def body_mode(self, name: str) -> BodyMode | None:
        """How to read this block's body, or ``None`` if the schema is unknown."""
        schema = self._blocks.get(name)
        return schema.body if schema is not None else None

    def default_item_schema(self, block_name: str) -> str | None:
        """The item name a homogeneous block's lines may omit."""
        schema = self._blocks.get(block_name)
        if schema is None:
            return None
        item = schema.item_model()
        return getattr(item, "schema_name", None) if item is not None else None

    def should_emit_text(self, name: str) -> bool:
        """Whether a text-mode block emits ui.text events."""
        schema = self._blocks.get(name)
        if schema is not None and hasattr(schema, "should_emit_text"):
            return schema.should_emit_text()
        return True

    def parse_line(self, name: str, line: str, block: Any) -> Mapping[str, Any] | None:
        """Extract structured items from a line in a text body."""
        schema = self._blocks.get(name)
        if schema is not None and hasattr(schema, "parse_line"):
            return schema.parse_line(line, block)
        return None

    def set_on_complete(self, name: str, handler: OnCompleteHandler) -> CardCatalog:
        self._on_complete[name] = handler
        return self

    def on_complete(self, name: str) -> Callable[[OnCompleteHandler], OnCompleteHandler]:
        def decorator(handler: OnCompleteHandler) -> OnCompleteHandler:
            self.set_on_complete(name, handler)
            return handler

        return decorator

    def has_on_complete(self, name: str) -> bool:
        if name in self._on_complete:
            return True
        schema = self._blocks.get(name)
        return schema is not None and hasattr(schema, "on_complete")

    async def complete(self, name: str, block: Any, scope: Any = None) -> Mapping[str, Any] | None:
        handler = self._on_complete.get(name)
        if handler is not None:
            res = handler(block, scope)
            if inspect.isawaitable(res):
                res = await res
            return res
        schema = self._blocks.get(name)
        if schema is not None and hasattr(schema, "on_complete"):
            res = schema.on_complete(block, scope)
            if inspect.isawaitable(res):
                res = await res
            return res
        return None

    # ── validation ────────────────────────────────────────────────────────

    def validate_props(
        self, name: str, props: Mapping[str, Any]
    ) -> tuple[dict[str, Any], str | None]:
        schema = self._blocks.get(name)
        if schema is None:
            if self.allow_unknown:
                return dict(props), None
            return dict(props), f"unknown block schema {name!r}"
        return self._validate(schema.props_model(), props)

    def validate_item(self, name: str, data: Any) -> tuple[Any, str | None]:
        if not isinstance(data, Mapping):
            return data, "an item must be a JSON object"
        model = self._items.get(name) or self._blocks.get(name)
        if model is None:
            if self.allow_unknown:
                return dict(data), None
            return dict(data), f"unknown item schema {name!r}"
        target = model.item_model() if issubclass(model, BlockSchema) else model
        if target is None:
            return dict(data), f"block schema {name!r} declares no item type"
        return self._validate(target, data)

    def _validate(
        self, model: type[BaseModel], data: Mapping[str, Any]
    ) -> tuple[dict[str, Any], str | None]:
        try:
            instance = model.model_validate(
                dict(data),
                context={"allowed_media_domains": self.allowed_media_domains},
            )
        except ValidationError as exc:
            return dict(data), _first_error(exc)
        return instance.model_dump(mode="json"), None

    # ── resolvers ─────────────────────────────────────────────────────────

    def resolver(self, name: str) -> Callable[[Resolver], Resolver]:
        """Register the function that turns an item's identifiers into facts."""

        def decorate(fn: Resolver) -> Resolver:
            self.set_resolver(name, fn)
            return fn

        return decorate

    def set_resolver(self, name: str, fn: Resolver) -> CardCatalog:
        self._resolvers[name] = fn
        return self

    def resolvers(self) -> dict[str, Resolver]:
        return dict(self._resolvers)

    def replace_resolvers(self, resolvers: Mapping[str, Resolver]) -> CardCatalog:
        """Swap the whole resolver set, for tests and golden traces.

        A resolver calls a real API, which would make any recorded trace
        non-deterministic. Replacing the set wholesale is how a test pins the
        data without the production catalog knowing that tests exist.
        """
        self._resolvers = dict(resolvers)
        return self

    def has_resolver(self, name: str) -> bool:
        if name in self._resolvers:
            return True
        model = self._items.get(name) or self._blocks.get(name)
        return model is not None and getattr(model, "resolve", None) is not ItemSchema.resolve

    async def resolve(self, name: str, data: Any, ctx: Any = None) -> Mapping[str, Any] | None:
        resolver = self._resolvers.get(name)
        if resolver is not None:
            raw_res = resolver(data)
            return await raw_res if inspect.isawaitable(raw_res) else raw_res

        model = self._items.get(name)
        if model is None and name in self._blocks:
            model = self._blocks[name]

        if model is not None:
            try:
                if isinstance(data, dict):
                    instance = model.model_validate(
                        data, context={"allowed_media_domains": self.allowed_media_domains}
                    )
                elif isinstance(data, model):
                    instance = data
                else:
                    instance = None

                if instance is not None and hasattr(instance, "resolve"):
                    maybe_coro = instance.resolve(ctx)
                    resolved_dict = (
                        await maybe_coro if inspect.isawaitable(maybe_coro) else maybe_coro
                    )
                    if resolved_dict is not None:
                        return cast(Mapping[str, Any], resolved_dict)
            except Exception:
                pass
        return None

    # ── multi-target rendering ───────────────────────────────────────────

    def render_item(
        self,
        platform: str,
        schema_name: str,
        data: Mapping[str, Any],
        resolved: Mapping[str, Any] | None = None,
    ) -> Any:
        """Render a single item fragment for the requested platform.

        Supported platforms include 'teams', 'lark', 'cli'. If the item class
        provides a `render_{platform}` method, that is invoked; otherwise, an
        automatic fallback layout is returned.
        """
        model = self._items.get(schema_name)
        if model is None and schema_name in self._blocks:
            model = self._blocks[schema_name]

        instance = None
        if model is not None and isinstance(data, dict):
            with contextlib.suppress(Exception):
                instance = model.model_validate(
                    data, context={"allowed_media_domains": self.allowed_media_domains}
                )

        method_name = f"render_{platform}"
        if instance is not None and hasattr(instance, method_name):
            custom_fn = getattr(instance, method_name)
            res = custom_fn(resolved or {})
            if res is not None:
                return res

        return self._fallback_item(platform, schema_name, data, resolved)

    def render_block(
        self,
        platform: str,
        block_name: str,
        props: Mapping[str, Any],
        rendered_items: list[Any],
    ) -> Any:
        """Render an entire block (bundling child items) for the requested platform."""
        schema = self._blocks.get(block_name)
        instance = None
        if schema is not None and isinstance(props, dict):
            with contextlib.suppress(Exception):
                instance = schema.props_model().model_validate(props)

        method_name = f"render_{platform}_block"
        if instance is not None and hasattr(instance, method_name):
            custom_fn = getattr(instance, method_name)
            res = custom_fn(rendered_items)
            if res is not None:
                return res

        return self._fallback_block(platform, block_name, props, rendered_items)

    def _fallback_item(
        self,
        platform: str,
        schema_name: str,
        data: Mapping[str, Any],
        resolved: Mapping[str, Any] | None,
    ) -> Any:
        all_facts = {**dict(data), **(dict(resolved or {}))}
        title = (
            (resolved or {}).get("title")
            or all_facts.get("title")
            or f"{schema_name} #{all_facts.get('id', '')}".strip()
        )

        if platform == "teams":
            facts = []
            for k, v in all_facts.items():
                if v is not None and k not in ("id", "schema", "note", "title", "poster"):
                    facts.append({"title": k.replace("_", " ").title(), "value": str(v)})
            items: list[dict[str, Any]] = [
                {"type": "TextBlock", "text": str(title), "weight": "Bolder", "wrap": True}
            ]
            if "note" in all_facts and all_facts["note"]:
                items.append(
                    {
                        "type": "TextBlock",
                        "text": str(all_facts["note"]),
                        "isSubtle": True,
                        "wrap": True,
                    }
                )
            if facts:
                items.append({"type": "FactSet", "facts": facts})
            return {"type": "Container", "separator": True, "items": items}

        if platform == "lark":
            lines = [f"**{title}**"]
            if "note" in all_facts and all_facts["note"]:
                lines.append(f"_{all_facts['note']}_")
            for k, v in all_facts.items():
                if v is not None and k not in ("id", "schema", "note", "title", "poster"):
                    lines.append(f"• **{k.replace('_', ' ').title()}**: {v}")
            return {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}}

        # cli fallback
        lines = [f"• {title}"]
        if "note" in all_facts and all_facts["note"]:
            lines.append(f"  Note: {all_facts['note']}")
        for k, v in all_facts.items():
            if v is not None and k not in ("id", "schema", "note", "title", "poster"):
                lines.append(f"  - {k}: {v}")
        return "\n".join(lines)

    def _fallback_block(
        self,
        platform: str,
        block_name: str,
        props: Mapping[str, Any],
        rendered_items: list[Any],
    ) -> Any:
        title = props.get("title") or block_name.replace("-", " ").title()

        if platform == "teams":
            body: list[dict[str, Any]] = [
                {"type": "TextBlock", "text": str(title), "weight": "Bolder", "size": "Medium"}
            ]
            body.extend(rendered_items)
            return {
                "type": "AdaptiveCard",
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "version": "1.5",
                "body": body,
            }

        if platform == "lark":
            return {
                "schema": "2.0",
                "header": {"title": {"content": str(title), "tag": "plain_text"}},
                "body": {"elements": rendered_items},
            }

        # cli fallback
        divider = "=" * max(30, len(title) + 6)
        items_str = "\n".join(str(it) for it in rendered_items)
        return f"{divider}\n[ {title} ]\n{divider}\n{items_str}\n{divider}"

    # ── generated artefacts ───────────────────────────────────────────────

    def to_json_schema(self) -> dict[str, Any]:
        """JSON Schema for every registered shape, for the frontend and tooling."""
        return {
            "blocks": {
                name: {
                    "body": schema.body,
                    "item": self.default_item_schema(name),
                    "props": schema.props_model().model_json_schema(),
                }
                for name, schema in sorted(self._blocks.items())
            },
            "items": {
                name: model.model_json_schema() for name, model in sorted(self._items.items())
            },
        }

    def to_prompt(
        self,
        *,
        include: Iterable[str] | None = None,
        exclude: Iterable[str] | None = None,
        system_prompt_only: bool = True,
    ) -> str:
        """The instructions to put in the system prompt.

        Generated rather than written by hand, because a hand-written copy is
        one edit away from describing a card the server no longer accepts.

        Parameters
        ----------
        include:
            Optional whitelist of block schema names to document. When supplied,
            only these registered blocks are included.
        exclude:
            Optional blacklist of block schema names to skip.
        system_prompt_only:
            When True (default) and ``include`` is None, only schemas with
            ``include_in_system_prompt = True`` are documented. When ``include``
            is explicitly provided, it specifies the desired cards directly,
            so ``system_prompt_only`` is bypassed.
        """
        if not self._blocks:
            return ""

        include_set = set(include) if include is not None else None
        exclude_set = set(exclude) if exclude is not None else set()

        names: list[str] = []
        for name in sorted(self._blocks):
            if name in exclude_set:
                continue
            if include_set is not None:
                if name not in include_set:
                    continue
            elif system_prompt_only:
                schema = self._blocks[name]
                if not getattr(schema, "include_in_system_prompt", True):
                    continue
            names.append(name)

        if not names:
            return ""

        lines = [_PROMPT_PREAMBLE.strip(), ""]
        for name in names:
            lines.extend(self._describe_block(name))
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def _describe_block(self, name: str) -> list[str]:
        schema = self._blocks[name]
        body = schema.body
        out = [f"### {name}"]
        doc = _docstring_of(schema)
        if doc:
            out.append(doc)

        props = _describe_fields(schema.props_model())
        out.append(f"Header properties: {props}" if props else "Header properties: none")

        if body == "text":
            out.append("Body: raw text, written exactly as it should appear.")
        else:
            item = schema.item_model()
            if item is not None:
                out.append(
                    f"Body: one JSON object per line, each {_describe_fields(item) or 'empty'}. "
                    'The "schema" key may be omitted — every line is a '
                    f'"{getattr(item, "schema_name", name)}".'
                )
            else:
                out.append('Body: one JSON object per line, each carrying its own "schema" key.')

        out.append("Example:")
        out.append(_example_for(schema))
        return out

    def __repr__(self) -> str:
        return f"<CardCatalog blocks={self.block_names} items={self.item_names}>"


_PROMPT_PREAMBLE = """
## Rich UI blocks

Beyond plain prose you can render cards, by writing an XML block into your answer:

<stream-ui>
{"schema": "<name>", ...properties}
...body...
</stream-ui>

- Open the block with `<stream-ui>` on its own line.
- The first line inside must be a single JSON object with "schema" and that block's properties.
- Close the block with `</stream-ui>` on its own line.
- Because it is delimited by `<stream-ui> ... </stream-ui>`, you can freely use markdown code blocks (```python, etc.) inside the body without escaping.
- Only use the schemas listed below, and only their listed fields.
- Ordinary code you simply want displayed belongs in a normal ```language fence.
  A code card is for when you need the filename, highlighted lines or diff stats.
"""


def _docstring_of(model: type[BaseModel]) -> str:
    doc = (model.__doc__ or "").strip().splitlines()
    return doc[0].strip() if doc else ""


def _describe_fields(model: type[BaseModel]) -> str:
    parts = []
    for field_name, field in model.model_fields.items():
        described = _describe_type(field.annotation)
        optional = "" if field.is_required() else ", optional"
        parts.append(f"{field_name} ({described}{optional})")
    return ", ".join(parts)


def _describe_type(annotation: Any) -> str:
    origin = get_origin(annotation)
    if origin is Literal:
        return "one of " + " | ".join(json.dumps(a) for a in get_args(annotation))
    if annotation is MediaUrl:
        return "https URL"
    if origin is list:
        inner = get_args(annotation)
        return f"list of {_describe_type(inner[0])}" if inner else "list"
    args = [a for a in get_args(annotation) if a is not type(None)]
    if origin is not None and args:
        if len(args) == 1:
            return _describe_type(args[0])
        return " or ".join(_describe_type(a) for a in args)
    return getattr(annotation, "__name__", str(annotation))


def _example_for(schema: type[BlockSchema]) -> str:
    header = {"schema": schema.schema_name, **_example_fields(schema.props_model())}
    lines = [
        "<stream-ui>",
        json.dumps(header, ensure_ascii=False),
    ]
    if schema.body == "text":
        lines.append("<the text, exactly as it should be shown>")
    else:
        item = schema.item_model()
        if item is not None:
            lines.append(json.dumps(_example_fields(item, required_only=False), ensure_ascii=False))
        else:
            lines.append('{"schema": "<item schema>", ...}')
    lines.append("</stream-ui>")
    return "\n".join(lines)


def _example_fields(model: type[BaseModel], *, required_only: bool = True) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for field_name, field in model.model_fields.items():
        if required_only and not field.is_required():
            continue
        out[field_name] = _example_value(field.annotation)
    return out


def _example_value(annotation: Any) -> Any:
    origin = get_origin(annotation)
    if origin is Literal:
        choices = get_args(annotation)
        return choices[0] if choices else "..."
    args = [a for a in get_args(annotation) if a is not type(None)]
    if origin is list:
        return [_example_value(args[0])] if args else []
    if origin is not None and args:
        return _example_value(args[0])
    if annotation is int:
        return 0
    if annotation is float:
        return 0.0
    if annotation is bool:
        return True
    if annotation is MediaUrl:
        return "https://..."
    return "..."


def _first_error(exc: ValidationError) -> str:
    error = exc.errors()[0]
    location = ".".join(str(part) for part in error.get("loc", ())) or "(root)"
    return f"{location}: {error.get('msg', 'invalid')}"


__all__ = [
    "BlockSchema",
    "BodyMode",
    "CardCatalog",
    "CardSchema",
    "CatalogConflict",
    "ItemSchema",
    "MediaUrl",
    "Resolver",
]
