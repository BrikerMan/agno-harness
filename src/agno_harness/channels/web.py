from typing import Any

from ..api_paths import CHANNELS_PATH
from ..core.channel import OutboundMessage
from ..sessions.models import ConversationKey
from .base import BaseChannel

#: Chat API for the web channel. Same shape as the Teams webhook path.
WEB_AGUI_PATH = f"{CHANNELS_PATH}/web/agui"


class WebChannel(BaseChannel):
    """Web AG-UI transport channel serving Server-Sent Events over FastAPI."""

    def __init__(self, prefix: str = WEB_AGUI_PATH, runtime: Any = None) -> None:
        super().__init__(name="web")
        self.prefix = prefix
        self.runtime = runtime
        self._router: Any = None

    def get_router(self, runtime: Any = None) -> Any:
        """Create or return the FastAPI APIRouter for AG-UI."""
        rt = runtime or self.runtime
        if rt is None:
            raise ValueError("AgentRuntime must be provided to WebChannel to build the router.")
        if self._router is None:
            from ..transport.router import make_agui_router

            self._router = make_agui_router(rt)
        return self._router

    async def send(self, destination: ConversationKey, message: OutboundMessage) -> str:
        """Web channel primarily delivers events over live SSE stream; send is a fallback."""
        return f"web_msg_{destination.chat_id}"
