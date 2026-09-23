"""Teams card documents that are not the generic text shell."""

from __future__ import annotations

from typing import Any


def fragment_text(items: list[Any]) -> str:
    """Join text and code fragments the collector already rendered."""
    parts: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            if item:
                parts.append(str(item))
            continue
        text = item.get("text") or item.get("codeSnippet") or ""
        if text:
            parts.append(str(text))
    return "\n".join(parts).strip()


def split_event_text(text: str) -> tuple[str, str]:
    """Read ``Time:`` and ``Event:`` lines. Other lines stay in the event field."""
    when = ""
    what_lines: list[str] = []
    event_lines: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        lower = line.lower()
        if lower.startswith("time:"):
            when = line.split(":", 1)[1].strip()
        elif lower.startswith("event:"):
            event_lines.append(line.split(":", 1)[1].strip())
        elif line:
            what_lines.append(line)
    what = "\n".join(event_lines).strip() or "\n".join(what_lines).strip() or text.strip()
    return when, what


def teams_event_card(text: str, *, title: str = "Event") -> dict[str, Any]:
    """Accent title plus a FactSet, so an event is not another prose card."""
    when, what = split_event_text(text)
    return {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.5",
        "body": [
            {
                "type": "Container",
                "style": "accent",
                "bleed": True,
                "items": [
                    {
                        "type": "TextBlock",
                        "text": title,
                        "weight": "Bolder",
                        "size": "Medium",
                        "color": "Accent",
                    },
                    {
                        "type": "FactSet",
                        "facts": [
                            {"title": "Time", "value": when or "n/a"},
                            {"title": "Event", "value": what or "n/a"},
                        ],
                    },
                ],
            }
        ],
    }
