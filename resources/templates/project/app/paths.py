"""Runtime files live under data/, which .gitignore already ignores.

Extend
------
Seeds in app/knowledge/ are copied into data/knowledge/ when that file is missing.
The agent searches data/knowledge/, so an edit there is visible on the next turn.
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
KNOWLEDGE = DATA / "knowledge"
SEED_KNOWLEDGE = ROOT / "app" / "knowledge"
AGENT_DB = DATA / "agent.db"

_MARKDOWN = {".md", ".markdown"}


def ensure_data() -> None:
    """Create the database file and the markdown knowledge directory.

    Agent history and channel sessions share ``data/agent.db``.
    """
    KNOWLEDGE.mkdir(parents=True, exist_ok=True)
    _copy_missing_seeds()
    AGENT_DB.parent.mkdir(parents=True, exist_ok=True)
    if not AGENT_DB.exists():
        sqlite3.connect(AGENT_DB).close()


def _copy_missing_seeds() -> None:
    if not SEED_KNOWLEDGE.is_dir():
        return
    for src in sorted(SEED_KNOWLEDGE.rglob("*")):
        if not src.is_file() or src.suffix.lower() not in _MARKDOWN:
            continue
        dest = KNOWLEDGE / src.relative_to(SEED_KNOWLEDGE)
        if dest.exists():
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
