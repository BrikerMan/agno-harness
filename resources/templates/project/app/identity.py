"""Who is calling.

MUST CHANGE BEFORE PRODUCTION.
Both resolvers below are local defaults. Replace them with your real
session, JWT, or directory check before this process serves anyone but you.
"""

from collections.abc import Callable

from fastapi import Request

UserResolver = Callable[[Request], str | None]


def build_user_resolver() -> UserResolver:
    """Default caller identity for web and CLI-adjacent HTTP.

    MUST CHANGE BEFORE PRODUCTION.
    This trusts the X-User-Id header and otherwise treats every caller as one
    shared local user. Anyone who can reach the port can read every thread.
    """

    def resolve_user_id(request: Request) -> str | None:
        # MUST CHANGE BEFORE PRODUCTION.
        return request.headers.get("X-User-Id") or "local-dev"

    return resolve_user_id


def build_teams_resolver() -> UserResolver:
    """Caller identity when this process serves Microsoft Teams.

    MUST CHANGE BEFORE PRODUCTION.
    TeamsChannel already verified the Bot Framework JWT and stored the AAD
    object id on the activity. This function is only the AG-UI router.
    It prefers the Teams principal header and does not check a token.
    Replace it with your directory lookup before you go live.
    """

    def resolve_user_id(request: Request) -> str | None:
        # MUST CHANGE BEFORE PRODUCTION.
        return (
            request.headers.get("X-Ms-Client-Principal-Id")
            or request.headers.get("X-User-Id")
            or "local-dev"
        )

    return resolve_user_id
