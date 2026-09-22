"""FastAPI composition root.

Extend
------
This is the only place that builds the production app.
Channels are mounted from app.channels.mount_all.
Button handlers are registered from app.agents.main.actions.
"""

from fastapi import FastAPI

from agno_harness import setup_relay_logging
from app.agents.main.actions import register as register_actions
from app.channels import mount_all
from app.runtime_factory import build_relay


def create_app() -> FastAPI:
    setup_relay_logging(logger_names=["agno_harness", "ag_ui", "app"])
    relay = build_relay()
    register_actions(relay)
    app = FastAPI(title="Agent", lifespan=relay.lifespan)
    mount_all(relay, app)
    return app
