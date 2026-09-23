"""Who is calling.

Extend
------
Both resolvers are local defaults.
build_user_resolver trusts the X-User-Id header and otherwise treats every
caller as one shared local user. Anyone who can reach the port can read
every thread.
build_teams_resolver reads the Teams principal header. TeamsChannel already
verified the Bot Framework JWT. This function does not check a token.
Replace them with your session, JWT, or directory check before this process
serves anyone but you.
"""

from collections.abc import Callable

from fastapi import Request

UserResolver = Callable[[Request], str | None]


def build_user_resolver() -> UserResolver:
    """Caller identity for web and CLI-adjacent HTTP."""

    def resolve_user_id(request: Request) -> str | None:
        return request.headers.get("X-User-Id") or "local-dev"

    return resolve_user_id


def build_teams_resolver() -> UserResolver:
    """Caller identity when this process serves Microsoft Teams."""

    def resolve_user_id(request: Request) -> str | None:
        return (
            request.headers.get("X-Ms-Client-Principal-Id")
            or request.headers.get("X-User-Id")
            or "local-dev"
        )

    return resolve_user_id
