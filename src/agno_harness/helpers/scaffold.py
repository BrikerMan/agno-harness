"""Copy the project template shipped in ``resources/templates/``.

The template is a real project so formatters and linters can read it.
``--channel`` only swaps ``agent.py`` and ``app/channels/__init__.py``.
"""

from __future__ import annotations

import shutil
from pathlib import Path

CHANNELS = ("all", "cli", "web", "teams", "lark")


def template_root() -> Path:
    """Directory copied by ``agno-harness init``."""
    repo = Path(__file__).resolve().parents[3] / "resources" / "templates" / "project"
    if (repo / "app").is_dir():
        return repo
    bundled = Path(__file__).resolve().parents[1] / "project_template"
    if (bundled / "app").is_dir():
        return bundled
    raise FileNotFoundError("project template not found")


def _variants_root() -> Path:
    repo = Path(__file__).resolve().parents[3] / "resources" / "templates" / "variants"
    if repo.is_dir():
        return repo
    bundled = Path(__file__).resolve().parents[1] / "project_template_variants"
    if bundled.is_dir():
        return bundled
    raise FileNotFoundError("project template variants not found")


def copy_project(target: Path, *, channel: str) -> None:
    """Copy the template into ``target``. Existing files are left in place."""
    selected = channel.lower().strip()
    if selected not in CHANNELS:
        raise ValueError(f"Unknown channel {channel!r}. Choose {', '.join(CHANNELS)}.")
    root = template_root()
    variants = _variants_root()
    for src in root.rglob("*"):
        if not src.is_file() or "__pycache__" in src.parts:
            continue
        rel = src.relative_to(root)
        dest = target / rel
        if dest.exists():
            continue
        source = _variant_source(rel.as_posix(), selected, variants) or src
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, dest)


def _variant_source(relative: str, channel: str, variants: Path) -> Path | None:
    if relative == "agent.py" and channel == "cli":
        return variants / "agent_cli.py"
    if relative == "app/channels/__init__.py" and channel != "all":
        return variants / f"mounts_{channel}.py"
    return None
