"""Skill definition model and Markdown frontmatter parser."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import SkillParseError

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment]


_FRONTMATTER_PATTERN = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)


@dataclass
class SkillDefinition:
    """A skill definition loaded from Markdown frontmatter, file, or string.

    Attributes
    ----------
    name:
        Unique identifier for the skill (e.g. 'financial_audit').
    description:
        Short 1-2 sentence description of what the skill does, used in the
        system prompt's skill roster.
    instructions:
        The markdown instructions body guiding the agent on how to use this skill.
    cards:
        List of card schema names declared in frontmatter that this skill needs.
    source_path:
        Path to the source file or directory, or '<string>' if loaded from memory.
    metadata:
        Additional arbitrary metadata parsed from frontmatter.
    """

    name: str
    description: str
    instructions: str
    cards: list[str] = field(default_factory=list)
    source_path: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_markdown(cls, text: str, source_path: str = "<string>") -> SkillDefinition:
        """Parse raw markdown text with optional YAML frontmatter.

        Raises
        ------
        SkillParseError
            If frontmatter syntax is invalid or not a dictionary.
        """
        frontmatter_data, instructions = _parse_frontmatter(text)

        name = str(frontmatter_data.get("name", "")).strip()
        description = str(frontmatter_data.get("description", "")).strip()

        raw_cards = frontmatter_data.get("cards", [])
        cards: list[str] = []
        if isinstance(raw_cards, list):
            cards = [str(c).strip() for c in raw_cards if str(c).strip()]
        elif isinstance(raw_cards, str):
            cards = [c.strip() for c in raw_cards.split(",") if c.strip()]

        explicit_metadata = frontmatter_data.get("metadata")
        metadata: dict[str, Any] = {}
        if isinstance(explicit_metadata, dict):
            metadata.update(explicit_metadata)

        for k, v in frontmatter_data.items():
            if k not in {"name", "description", "cards", "metadata"}:
                metadata[k] = v

        return cls(
            name=name,
            description=description,
            instructions=instructions.strip(),
            cards=cards,
            source_path=source_path,
            metadata=metadata,
        )

    @classmethod
    def from_file(cls, path: str | Path) -> SkillDefinition:
        """Load and parse a skill from a Markdown file."""
        file_path = Path(path).resolve()
        if not file_path.exists():
            raise FileNotFoundError(f"Skill file not found: {file_path}")
        content = file_path.read_text(encoding="utf-8")
        return cls.from_markdown(content, source_path=str(file_path))


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Extract YAML frontmatter and body from markdown text."""
    match = _FRONTMATTER_PATTERN.match(text)
    if not match:
        return {}, text.strip()

    frontmatter_text = match.group(1)
    body = match.group(2)

    if yaml is not None:
        try:
            data = yaml.safe_load(frontmatter_text) or {}
        except Exception as exc:
            raise SkillParseError(f"Invalid YAML in frontmatter: {exc}") from exc
    else:
        data = _fallback_parse_frontmatter(frontmatter_text)

    if not isinstance(data, dict):
        raise SkillParseError(f"Frontmatter must be a key-value mapping, got {type(data).__name__}")

    return data, body


def _fallback_parse_frontmatter(text: str) -> dict[str, Any]:
    """Lightweight fallback parser when PyYAML is unavailable."""
    result: dict[str, Any] = {}
    current_list_key: str | None = None

    for line in text.splitlines():
        trimmed = line.strip()
        if not trimmed or trimmed.startswith("#"):
            continue

        if trimmed.startswith("- ") and current_list_key is not None:
            item_val = trimmed[2:].strip().strip('"').strip("'")
            result[current_list_key].append(item_val)
            continue

        if ":" in line:
            current_list_key = None
            key, val = line.split(":", 1)
            key = key.strip()
            val = val.strip()
            if not val:
                result[key] = []
                current_list_key = key
            elif val.startswith("[") and val.endswith("]"):
                inner = val[1:-1].strip()
                result[key] = [
                    item.strip().strip('"').strip("'") for item in inner.split(",") if item.strip()
                ]
            else:
                result[key] = val.strip('"').strip("'")

    return result
