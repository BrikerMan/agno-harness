"""Channels mounted by create_app.

This process entry is the terminal, which calls app.channels.cli.mount directly.
create_app is still here. Add mount calls below when this process should serve HTTP.

Extend
------
Add app/channels/<name>.py with mount(relay, app).
Import that function and call it from mount_all.
mount() logs an error and raises when its settings are missing.
"""


def mount_all(relay, app) -> None:
    del relay, app
