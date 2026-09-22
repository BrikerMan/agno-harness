"""Channels mounted by create_app.

Extend
------
Add app/channels/<name>.py with mount(relay, app).
Import that function and call it from mount_all.
mount() logs an error and raises when its settings are missing.
mount_web calls get_router(), so register other channels before it.
This process includes Teams, so the AG-UI router uses the Teams resolver.
"""

from app.channels.cli import mount as mount_cli
from app.channels.lark import mount as mount_lark
from app.channels.teams import mount as mount_teams
from app.channels.web import mount as mount_web
from app.identity import build_teams_resolver


def mount_all(relay, app) -> None:
    mount_teams(relay, app)
    mount_lark(relay, app)
    mount_cli(relay, app)
    mount_web(relay, app, resolve_user_id=build_teams_resolver())
