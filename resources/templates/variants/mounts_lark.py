"""Channels mounted by create_app.

Extend
------
Add app/channels/<name>.py with mount(relay, app).
Import that function and call it from mount_all.
mount() logs an error and raises when its settings are missing.
"""

from app.channels.lark import mount as mount_lark


def mount_all(relay, app) -> None:
    mount_lark(relay, app)
