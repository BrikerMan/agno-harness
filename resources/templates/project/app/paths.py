"""Runtime files live under data/, which .gitignore already ignores.

Extend
------
Seeds in app/knowledge/ and app/learning/ are copied into data/ when missing.
The agent searches data/knowledge/ and learns into data/learning/.
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
KNOWLEDGE = DATA / "knowledge"
SEED_KNOWLEDGE = ROOT / "app" / "knowledge"
LEARNING = DATA / "learning"
SEED_LEARNING = ROOT / "app" / "learning"
AGENT_DB = DATA / "agent.db"
AUDIT_DB = DATA / "audit.db"

_MARKDOWN = {".md", ".markdown"}


def ensure_data() -> None:
    """Create the database file and knowledge/learning directories."""
    KNOWLEDGE.mkdir(parents=True, exist_ok=True)
    LEARNING.mkdir(parents=True, exist_ok=True)
    _copy_missing_seeds(SEED_KNOWLEDGE, KNOWLEDGE)
    _copy_missing_seeds(SEED_LEARNING, LEARNING)
    AGENT_DB.parent.mkdir(parents=True, exist_ok=True)
    if not AGENT_DB.exists():
        sqlite3.connect(AGENT_DB).close()


def _copy_missing_seeds(seed_root: Path, live_root: Path) -> None:
    if not seed_root.is_dir():
        return
    for src in sorted(seed_root.rglob("*")):
        if not src.is_file() or src.suffix.lower() not in _MARKDOWN:
            continue
        dest = live_root / src.relative_to(seed_root)
        if dest.exists():
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
