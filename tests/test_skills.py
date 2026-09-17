"""Tests for SkillManager, SkillDefinition, validation, and dynamic card injection."""

from pathlib import Path

import pytest

from agno_relay import (
    BlockSchema,
    CardCatalog,
    ItemSchema,
    SkillDefinition,
    SkillManager,
    SkillParseError,
    SkillValidationError,
)


class Stat(ItemSchema):
    schema_name = "stat"
    label: str
    value: str


class StatRow(BlockSchema):
    schema_name = "stat-row"
    item = Stat


class TableRow(ItemSchema):
    schema_name = "row"
    cells: list[str]


class Table(BlockSchema):
    schema_name = "table"
    item = TableRow


class ArtifactCard(BlockSchema):
    schema_name = "artifact"
    body = "text"


def make_catalog() -> CardCatalog:
    cat = CardCatalog([StatRow, Table, ArtifactCard])
    return cat


def test_parse_skill_from_markdown():
    md = """---
name: financial_audit
description: 审计财报资产负债表与利润表
cards:
  - stat-row
  - table
metadata:
  version: "1.0"
---
# Financial Audit Guidelines
Follow these instructions carefully.
"""
    skill = SkillDefinition.from_markdown(md)
    assert skill.name == "financial_audit"
    assert skill.description == "审计财报资产负债表与利润表"
    assert skill.cards == ["stat-row", "table"]
    assert skill.metadata == {"version": "1.0"}
    assert "Follow these instructions carefully." in skill.instructions


def test_parse_skill_cards_as_comma_separated_string():
    md = """---
name: quick_stats
description: 快速统计
cards: stat-row, table
---
Instructions here.
"""
    skill = SkillDefinition.from_markdown(md)
    assert skill.cards == ["stat-row", "table"]


def test_parse_skill_invalid_yaml():
    md = """---
name: bad
cards: [unclosed
---
body
"""
    with pytest.raises(SkillParseError):
        SkillDefinition.from_markdown(md)


def test_skill_manager_from_string_and_strict_validation(tmp_path):
    cat = make_catalog()

    valid_md = """---
name: finance
description: Finance audit
cards:
  - stat-row
---
Do finance stuff.
"""
    manager = SkillManager(catalog=cat, sources=[valid_md], strict=True)
    assert len(manager) == 1
    assert "finance" in manager
    assert manager.get_skill("finance").cards == ["stat-row"]


def test_skill_manager_strict_mode_fails_on_unknown_card():
    cat = make_catalog()

    bad_card_md = """---
name: crypto_trader
description: Trades crypto
cards:
  - nonexistent_card_schema
---
Instructions.
"""
    with pytest.raises(SkillValidationError) as exc_info:
        SkillManager(catalog=cat, sources=[bad_card_md], strict=True)

    err = str(exc_info.value)
    assert "crypto_trader" in err
    assert "nonexistent_card_schema" in err


def test_skill_manager_non_strict_mode_logs_warnings():
    cat = make_catalog()

    bad_card_md = """---
name: crypto_trader
description: Trades crypto
cards:
  - nonexistent_card_schema
---
Instructions.
"""
    # non-strict mode should not raise
    manager = SkillManager(catalog=cat, sources=[bad_card_md], strict=False)
    assert len(manager) == 1
    assert "crypto_trader" in manager


def test_skill_manager_strict_mode_fails_on_missing_name_or_description():
    cat = make_catalog()

    missing_desc = """---
name: missing_desc
---
Instructions.
"""
    with pytest.raises(SkillValidationError) as exc_info:
        SkillManager(catalog=cat, sources=[missing_desc], strict=True)

    assert "missing a required 'description'" in str(exc_info.value)


def test_skill_manager_duplicate_skill_names_detected():
    cat = make_catalog()

    s1 = """---
name: audit
description: First audit
---
Instructions 1.
"""
    s2 = """---
name: audit
description: Second audit
---
Instructions 2.
"""
    with pytest.raises(SkillValidationError) as exc_info:
        SkillManager(catalog=cat, sources=[s1, s2], strict=True)

    assert "Duplicate skill name 'audit'" in str(exc_info.value)


def test_skill_folder_scanning(tmp_path: Path):
    cat = make_catalog()

    # Create skill 1 in subfolder with SKILL.md
    skill1_dir = tmp_path / "skill_one"
    skill1_dir.mkdir()
    (skill1_dir / "SKILL.md").write_text(
        """---
name: skill_one
description: First directory skill
cards:
  - stat-row
---
Skill one body.
""",
        encoding="utf-8",
    )

    # Create skill 2 in subfolder with SKILL.md
    skill2_dir = tmp_path / "skill_two"
    skill2_dir.mkdir()
    (skill2_dir / "SKILL.md").write_text(
        """---
name: skill_two
description: Second directory skill
cards:
  - table
---
Skill two body.
""",
        encoding="utf-8",
    )

    # Put a non-skill README.md in root
    (tmp_path / "README.md").write_text("# Documentation\nNot a skill.", encoding="utf-8")

    manager = SkillManager(catalog=cat, sources=[tmp_path], strict=True)
    assert len(manager) == 2
    assert manager.get_skill_names() == ["skill_one", "skill_two"]


def test_skill_many_to_many_cards_sharing():
    cat = make_catalog()

    s1 = """---
name: sales
description: Sales team skill
cards:
  - stat-row
  - artifact
---
Sales instructions.
"""
    s2 = """---
name: marketing
description: Marketing team skill
cards:
  - stat-row
  - table
---
Marketing instructions.
"""
    manager = SkillManager(catalog=cat, sources=[s1, s2], strict=True)
    assert manager.get_skill("sales").cards == ["stat-row", "artifact"]
    assert manager.get_skill("marketing").cards == ["stat-row", "table"]


def test_to_roster_prompt_and_jit_load_skill():
    cat = make_catalog()

    s1 = """---
name: finance
description: Audits financial sheets
cards:
  - stat-row
  - table
---
Step 1: Check balance.
Step 2: Output summary.
"""
    manager = SkillManager(catalog=cat, sources=[s1], strict=True)

    # Test roster prompt
    roster = manager.to_roster_prompt()
    assert "## Available Skills" in roster
    assert "- `finance`: Audits financial sheets" in roster

    # Test JIT loading
    loaded = manager.load_skill("finance")
    assert "# Skill: finance" in loaded
    assert "Step 1: Check balance." in loaded
    assert "## UI Cards For This Skill" in loaded
    assert "### stat-row" in loaded
    assert "### table" in loaded
    # artifact was not requested by this skill, so it must not be in the prompt!
    assert "### artifact" not in loaded


def test_load_nonexistent_skill():
    manager = SkillManager()
    res = manager.load_skill("nonexistent")
    assert "Error: Skill 'nonexistent' not found" in res


def test_create_load_skill_tool():
    cat = make_catalog()
    s1 = """---
name: helper
description: General helper
---
Helper body.
"""
    manager = SkillManager(catalog=cat, sources=[s1], strict=True)
    tool_fn = manager.create_load_skill_tool()
    assert tool_fn is not None
    # In Agno, @tool returns a Function object or callable
    assert callable(tool_fn) or hasattr(tool_fn, "entrypoint") or hasattr(tool_fn, "name")
