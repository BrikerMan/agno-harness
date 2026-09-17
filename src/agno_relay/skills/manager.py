"""SkillManager — skill discovery, validation, roster generation, and JIT card injection."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from agno_relay.core.streamui.schema import CardCatalog

from .definition import SkillDefinition
from .errors import SkillValidationError

logger = logging.getLogger(__name__)


class SkillManager:
    """Manages skill discovery, validation, roster generation, and JIT card injection.

    Parameters
    ----------
    catalog:
        Optional CardCatalog instance. When provided, skill card declarations
        are validated against it and JIT prompt generation extracts the schema.
    sources:
        Initial sources to load skills from. Can be a single source or an iterable
        containing:
        - Path to a skill directory containing subdirectories with SKILL.md
        - Path to a single skill directory with SKILL.md
        - Path to a single markdown file (*.md)
        - Raw markdown text containing YAML frontmatter
        - Pre-built SkillDefinition instances
    strict:
        If True (default), validation failures (missing name/description, duplicate
        names, or unknown cards) raise SkillValidationError on initialization.
        If False, warnings are logged and invalid skills/cards are skipped.
    """

    def __init__(
        self,
        catalog: CardCatalog | None = None,
        sources: (
            Iterable[str | Path | SkillDefinition] | str | Path | SkillDefinition | None
        ) = None,
        *,
        strict: bool = True,
    ) -> None:
        self.catalog = catalog
        self.strict = strict
        self._skills: dict[str, SkillDefinition] = {}
        self._pending_errors: list[str] = []

        if sources is not None:
            if isinstance(sources, (str, Path, SkillDefinition)):
                sources = [sources]
            for source in sources:
                self.load_source(source)

        self.validate(raise_on_error=self.strict)

    # ── discovery & registration ──────────────────────────────────────────

    def load_source(self, source: str | Path | SkillDefinition) -> list[SkillDefinition]:
        """Load one source into the manager."""
        if isinstance(source, SkillDefinition):
            self.register_skill(source)
            return [source]

        if self._is_path(source):
            return self.register_path(Path(source))

        if isinstance(source, str):
            skill = self.register_markdown(source)
            return [skill]

        raise TypeError(f"Unsupported skill source type: {type(source).__name__}")

    def register_skill(self, skill: SkillDefinition) -> None:
        """Register a pre-built SkillDefinition."""
        if not skill.name:
            self._pending_errors.append(
                f"Skill from '{skill.source_path or '<memory>'}' has an empty or missing 'name'."
            )
            return

        if skill.name in self._skills:
            existing = self._skills[skill.name]
            self._pending_errors.append(
                f"Duplicate skill name '{skill.name}' in '{skill.source_path}' "
                f"(already registered from '{existing.source_path}')."
            )
            return

        self._skills[skill.name] = skill

    def register_markdown(self, text: str, source_path: str = "<string>") -> SkillDefinition:
        """Register a skill from raw Markdown text containing YAML frontmatter."""
        try:
            skill = SkillDefinition.from_markdown(text, source_path=source_path)
        except Exception as exc:
            self._pending_errors.append(f"Failed to parse skill from '{source_path}': {exc}")
            skill = SkillDefinition(
                name="",
                description="",
                instructions=text,
                source_path=source_path,
            )

        self.register_skill(skill)
        return skill

    def register_path(self, path: str | Path) -> list[SkillDefinition]:
        """Scan and register skills from a file or directory path."""
        p = Path(path).resolve()
        if not p.exists():
            self._pending_errors.append(f"Skill path does not exist: {p}")
            return []

        loaded: list[SkillDefinition] = []

        if p.is_file():
            try:
                skill = SkillDefinition.from_file(p)
                self.register_skill(skill)
                loaded.append(skill)
            except Exception as exc:
                self._pending_errors.append(f"Failed to load skill file '{p}': {exc}")
            return loaded

        # Directory handling
        single_skill_md = p / "SKILL.md"
        if single_skill_md.is_file():
            return self.register_path(single_skill_md)

        # Directory containing multiple skill folders or .md files
        for item in sorted(p.iterdir()):
            if item.name.startswith("."):
                continue

            if item.is_dir():
                sub_skill = item / "SKILL.md"
                if sub_skill.is_file():
                    loaded.extend(self.register_path(sub_skill))
            elif item.is_file() and item.suffix == ".md":
                # Skip general non-skill readme files without frontmatter
                if item.name.lower() == "readme.md":
                    try:
                        first_line = item.read_text(encoding="utf-8").strip()
                        if not first_line.startswith("---"):
                            continue
                    except Exception:
                        continue
                loaded.extend(self.register_path(item))

        return loaded

    @staticmethod
    def _is_path(source: Any) -> bool:
        if isinstance(source, Path):
            return True
        if isinstance(source, str):
            if "\n" in source or source.strip().startswith("---"):
                return False
            try:
                return Path(source).exists()
            except OSError:
                return False
        return False

    # ── inspection ────────────────────────────────────────────────────────

    def get_skill(self, name: str) -> SkillDefinition | None:
        """Get a registered skill by name."""
        return self._skills.get(name)

    def get_skill_names(self) -> list[str]:
        """Get names of all registered skills in sorted order."""
        return sorted(self._skills.keys())

    def get_all_skills(self) -> list[SkillDefinition]:
        """Get all registered skills."""
        return list(self._skills.values())

    def __contains__(self, name: str) -> bool:
        return name in self._skills

    def __len__(self) -> int:
        return len(self._skills)

    # ── validation ────────────────────────────────────────────────────────

    def validate(self, *, raise_on_error: bool | None = None) -> list[str]:
        """Validate all registered skills against frontmatter requirements and CardCatalog.

        Checks
        ------
        1. Non-empty 'name' and 'description'
        2. No duplicate skill names
        3. All declared cards exist in the bound CardCatalog (if catalog provided)

        Returns
        -------
        list[str]
            List of validation error strings.
        """
        errors = list(self._pending_errors)
        self._pending_errors.clear()

        for name, skill in self._skills.items():
            if not skill.description:
                errors.append(
                    f"Skill '{name}' in '{skill.source_path}' is missing a required 'description'."
                )

            if self.catalog is not None:
                for card_name in skill.cards:
                    if not self.catalog.knows_block(card_name):
                        known = sorted(self.catalog.block_names)
                        errors.append(
                            f"Skill '{name}' references unknown card schema '{card_name}'. "
                            f"Known schemas in CardCatalog: {known}"
                        )

        should_raise = self.strict if raise_on_error is None else raise_on_error
        if errors:
            if should_raise:
                raise SkillValidationError(errors)
            for err in errors:
                logger.warning("SkillManager validation warning: %s", err)

        return errors

    # ── prompt generation & JIT loading ───────────────────────────────────

    def to_roster_prompt(self) -> str:
        """Generate a concise prompt describing available skills to the agent.

        Provides progressive disclosure: the agent sees skill summaries and learns
        it can invoke `load_skill(name)` when the user's intent matches.
        """
        if not self._skills:
            return ""

        lines = [
            "## Available Skills",
            "",
            "You have access to specialized domain skills. When a task requires specialized expertise,",
            "call `load_skill(name)` with the skill's name to retrieve its full instructions and any",
            "specialized UI card schemas it provides.",
            "",
        ]
        for name in sorted(self._skills):
            skill = self._skills[name]
            lines.append(f"- `{name}`: {skill.description}")

        return "\n".join(lines).rstrip() + "\n"

    def load_skill(self, name: str) -> str:
        """Load full instructions and card documentation for a skill.

        This is typically returned by the `load_skill` tool to inject instructions
        into the agent context on-demand (JIT).
        """
        skill = self._skills.get(name)
        if skill is None:
            available = ", ".join(f"`{k}`" for k in sorted(self._skills)) or "none"
            return f"Error: Skill '{name}' not found. Available skills: {available}"

        sections = [
            f"# Skill: {skill.name}",
            "",
            skill.instructions,
        ]

        if skill.cards and self.catalog is not None:
            cards_prompt = self.catalog.to_prompt(include=skill.cards)
            if cards_prompt:
                sections.extend(
                    [
                        "",
                        "## UI Cards For This Skill",
                        cards_prompt,
                    ]
                )

        return "\n".join(sections).strip()

    def create_load_skill_tool(self, tool_name: str = "load_skill") -> Any:
        """Create an Agno-compatible `@tool` function for dynamically loading skills."""

        async def load_skill(name: str) -> str:
            """Load instructions and UI card schemas for a specialized skill.

            Args:
                name: The name of the skill to load (from Available Skills).

            Returns:
                Full skill guidance and specialized UI card formats.
            """
            return self.load_skill(name)

        load_skill.__name__ = tool_name
        try:
            from agno.tools import tool

            return tool(load_skill)
        except ImportError:
            return load_skill
