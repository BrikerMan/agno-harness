"""Exceptions for the SkillManager system."""

from __future__ import annotations


class SkillError(Exception):
    """Base exception for all skill-related errors."""


class SkillParseError(SkillError):
    """Raised when a skill markdown file or frontmatter fails to parse."""


class SkillValidationError(SkillError):
    """Raised when skills fail validation under strict mode."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = list(errors)
        msg = f"Skill validation failed with {len(errors)} error(s):\n" + "\n".join(
            f"  - {err}" for err in errors
        )
        super().__init__(msg)
