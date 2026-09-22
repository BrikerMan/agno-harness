"""Small helpers used by the sample tools.

Extend
------
Add functions the tools and card resolvers share. Keep I/O behind a function name.
"""

from datetime import UTC, datetime
from pathlib import Path


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def list_markdown(folder: Path) -> list[str]:
    if not folder.is_dir():
        return []
    return sorted(path.name for path in folder.glob("*.md"))
