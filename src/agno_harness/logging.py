"""Professional structured and colored logging service for agno-harness across all channels."""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from .config import RelayConfig

# ANSI color codes for terminal rendering
_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"

_COLORS = {
    "DEBUG": "\033[36m",  # Cyan
    "INFO": "\033[32m",  # Green
    "WARNING": "\033[33m",  # Yellow
    "ERROR": "\033[31m",  # Red
    "CRITICAL": "\033[1;41m",  # White on Red
}

_BADGES = {
    "lark": "\033[1;36m[LARK]\033[0m",
    "teams": "\033[1;34m[TEAMS]\033[0m",
    "web": "\033[1;32m[WEB]\033[0m",
    "cli": "\033[1;35m[CLI]\033[0m",
    "runtime": "\033[1;35m[RUNTIME]\033[0m",
    "app": "\033[1;37m[APP]\033[0m",
    "sink": "\033[1;33m[SINK]\033[0m",
    "store": "\033[1;33m[STORE]\033[0m",
}


class RelayConsoleFormatter(logging.Formatter):
    """Clean, high-visibility human-readable colored formatter with platform badges."""

    def format(self, record: logging.LogRecord) -> str:
        level_name = record.levelname
        color = _COLORS.get(level_name, "")
        timestamp = datetime.fromtimestamp(record.created, tz=UTC).strftime("%H:%M:%S.%f")[:-3]

        # Extract badge from logger name or record attribute
        logger_name = record.name.lower()
        badge = ""
        for tag, badge_code in _BADGES.items():
            if tag in logger_name:
                badge = f"{badge_code} "
                break

        # Level tag
        level_tag = f"{color}{level_name:<7}{_RESET}"

        # Context details (e.g. chat_id, event_id, session_id)
        ctx_parts = []
        if hasattr(record, "chat_id") and record.chat_id:
            ctx_parts.append(f"chat={record.chat_id}")
        if hasattr(record, "session_id") and record.session_id:
            ctx_parts.append(f"session={record.session_id[:8]}")
        if hasattr(record, "event_id") and record.event_id:
            ctx_parts.append(f"evt={record.event_id}")

        ctx_str = f" \033[2m[{' | '.join(ctx_parts)}]\033[0m" if ctx_parts else ""

        message = record.getMessage()

        base = f"\033[2m{timestamp}\033[0m {level_tag} {badge}\033[1m{record.name}\033[0m{ctx_str}: {message}"

        if record.exc_info:
            if not record.exc_text:
                record.exc_text = self.formatException(record.exc_info)
            base = f"{base}\n{record.exc_text}"

        return base


class RelayJsonFormatter(logging.Formatter):
    """Machine-readable JSON formatter for production container / cloud logging."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for attr in ("chat_id", "session_id", "event_id", "platform", "action_id"):
            if hasattr(record, attr):
                payload[attr] = getattr(record, attr)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def setup_relay_logging(
    level: str | int | None = None,
    *,
    fmt: str = "console",
    logger_names: list[str] | None = None,
) -> logging.Handler:
    """Initialize and configure professional logging across agno-harness.

    Parameters
    ----------
    level:
        Log level (e.g. "DEBUG", "INFO", "WARNING"). Defaults to `AGNO_HARNESS_LOG_LEVEL` or "INFO".
    fmt:
        "console" for colored terminal output; "json" for production cloud logs.
    logger_names:
        List of logger hierarchies to capture (defaults to `['agno_harness', 'ag_ui']`).
    """
    effective_level: int
    if level is None:
        level_str = RelayConfig.log_level(default="INFO")
        effective_level = getattr(logging, level_str, logging.INFO)
    elif isinstance(level, str):
        effective_level = getattr(logging, level.upper(), logging.INFO)
    else:
        effective_level = level

    handler = logging.StreamHandler(sys.stdout)
    if fmt == "json":
        handler.setFormatter(RelayJsonFormatter())
    else:
        handler.setFormatter(RelayConsoleFormatter())

    targets = logger_names or ["agno_harness", "ag_ui"]
    for name in targets:
        lgr = logging.getLogger(name)
        lgr.setLevel(effective_level)
        # Avoid duplicate handlers on re-configuration
        lgr.handlers = [handler]
        lgr.propagate = False

    return handler


def get_relay_logger(name: str) -> logging.Logger:
    """Return a logger scoped under the `agno_harness` namespace."""
    if not name.startswith("agno_harness"):
        name = f"agno_harness.{name}"
    return logging.getLogger(name)
