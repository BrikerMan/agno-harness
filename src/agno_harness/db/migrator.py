from __future__ import annotations

from typing import Any

from sqlalchemy import MetaData

from ..stores.sql_models import harness_metadata, register_harness_models


class AlembicMigrator:
    """Helper for integrating agno-harness persistence models into project Alembic migrations."""

    @classmethod
    def declare_models(cls, base: Any, prefix: str | None = None) -> dict[str, type[Any]]:
        """Declare harness tables onto the project's declarative Base.

        Parameters
        ----------
        base:
            Your project's declarative Base (e.g. ``Base = declarative_base()``).
        prefix:
            Table prefix (e.g. ``"ipv"``, ``"admin"``). Defaults to ``"agno-harness"``
            or the ``AGNO_HARNESS_TABLE_PREFIX`` environment variable.
        """
        return register_harness_models(prefix=prefix, base=base)

    @classmethod
    def get_metadata(cls, prefix: str | None = None, base: Any | None = None) -> MetaData:
        """Return SQLAlchemy MetaData containing the declared harness tables."""
        return harness_metadata(prefix=prefix, base=base)


__all__ = ["AlembicMigrator"]
