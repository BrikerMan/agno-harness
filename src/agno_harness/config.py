"""Environment configuration for agno-harness.

Each setting has exactly one key: ``AGNO_HARNESS_*``. No vendor aliases.
"""

from __future__ import annotations

import os


def get_env_str(name: str, default: str = "") -> str:
    val = os.getenv(name)
    if val is not None and val.strip():
        return val.strip()
    return default


def get_env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("true", "1", "yes", "on")


class RelayConfig:
    """Reads ``AGNO_HARNESS_*`` environment variables."""

    @classmethod
    def llm_base_url(cls, default: str = "") -> str:
        return get_env_str("AGNO_HARNESS_LLM_BASE_URL", default=default)

    @classmethod
    def llm_api_key(cls, default: str = "") -> str:
        return get_env_str("AGNO_HARNESS_LLM_API_KEY", default=default)

    @classmethod
    def llm_model(cls, default: str = "gpt-4o") -> str:
        return get_env_str("AGNO_HARNESS_LLM_MODEL", default=default)

    @classmethod
    def lark_app_id(cls, default: str = "") -> str:
        return get_env_str("AGNO_HARNESS_LARK_APP_ID", default=default)

    @classmethod
    def lark_app_secret(cls, default: str = "") -> str:
        return get_env_str("AGNO_HARNESS_LARK_APP_SECRET", default=default)

    @classmethod
    def lark_encrypt_key(cls, default: str = "") -> str:
        return get_env_str("AGNO_HARNESS_LARK_ENCRYPT_KEY", default=default)

    @classmethod
    def lark_verification_token(cls, default: str = "") -> str:
        return get_env_str("AGNO_HARNESS_LARK_VERIFICATION_TOKEN", default=default)

    @classmethod
    def lark_use_websocket(cls, default: bool = True) -> bool:
        return get_env_bool("AGNO_HARNESS_LARK_USE_WEBSOCKET", default=default)

    @classmethod
    def teams_app_id(cls, default: str = "") -> str:
        return get_env_str("AGNO_HARNESS_TEAMS_APP_ID", default=default)

    @classmethod
    def teams_app_password(cls, default: str = "") -> str:
        return get_env_str("AGNO_HARNESS_TEAMS_APP_PASSWORD", default=default)

    @classmethod
    def teams_tenant_id(cls, default: str = "") -> str:
        return get_env_str("AGNO_HARNESS_TEAMS_TENANT_ID", default=default)

    @classmethod
    def log_level(cls, default: str = "INFO") -> str:
        return get_env_str("AGNO_HARNESS_LOG_LEVEL", default=default).upper()

    @classmethod
    def database_url(cls, default: str = "") -> str:
        return get_env_str("AGNO_HARNESS_DATABASE_URL", default=default)

    @classmethod
    def redis_url(cls, default: str = "") -> str:
        return get_env_str("AGNO_HARNESS_REDIS_URL", default=default)

    @classmethod
    def timezone(cls, default: str = "") -> str:
        return get_env_str("AGNO_HARNESS_TIMEZONE", default=default)
