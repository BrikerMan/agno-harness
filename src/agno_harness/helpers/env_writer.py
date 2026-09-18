"""Safe environment variable file writer and updater."""

from __future__ import annotations

from pathlib import Path


def save_env_file(updates: dict[str, str], filepath: str = ".env") -> Path:
    """Save or update key-value pairs in a .env file without deleting existing keys."""
    target_path = Path(filepath)
    existing_lines: list[str] = []
    keys_found: set[str] = set()

    if target_path.exists():
        with open(target_path, encoding="utf-8") as f:
            existing_lines = f.readlines()

    new_lines: list[str] = []
    for line in existing_lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key, _ = stripped.split("=", 1)
            key = key.strip()
            if key in updates:
                new_lines.append(f"{key}={updates[key]}\n")
                keys_found.add(key)
                continue
        new_lines.append(line)

    # Append any keys that weren't already in the file
    appended: list[str] = []
    for k, v in updates.items():
        if k not in keys_found:
            appended.append(f"{k}={v}\n")

    if appended:
        if new_lines and not new_lines[-1].endswith("\n"):
            new_lines.append("\n")
        new_lines.extend(appended)

    with open(target_path, "w", encoding="utf-8") as f:
        f.writelines(new_lines)

    return target_path.resolve()
