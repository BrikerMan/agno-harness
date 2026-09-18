"""transport — the optional HTTP layer.

This is the only package that imports FastAPI. Everything below it runs the same
whether the caller is a web server, a worker or a test, which is what makes the
"drive the agent yourself" path in the README possible.
"""

from .router import PROTOCOL_HEADER, RESUME_HEADER, SSE_HEADERS, make_agui_router

__all__ = ["PROTOCOL_HEADER", "RESUME_HEADER", "SSE_HEADERS", "make_agui_router"]
