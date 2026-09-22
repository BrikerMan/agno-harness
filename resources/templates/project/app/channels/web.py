"""Web channel. Chat API is POST /agui.

Extend
------
mount() receives the FastAPI app because this transport is HTTP.
A missing app is a programming error: log it and raise.
The default resolver is app.identity.build_user_resolver.
A Teams process passes build_teams_resolver instead.
"""

import logging

from app.identity import build_user_resolver

log = logging.getLogger(__name__)


def mount(relay, app, *, resolve_user_id=None) -> None:
    if app is None:
        message = "Web channel is enabled but no FastAPI app was provided."
        log.error(message)
        raise RuntimeError(message)
    # MUST CHANGE BEFORE PRODUCTION: replace the resolver before you go live.
    resolver = resolve_user_id or build_user_resolver()
    app.include_router(relay.get_router(resolve_user_id=resolver))
