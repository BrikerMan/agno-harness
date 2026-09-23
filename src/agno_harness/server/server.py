"""Class-based FastAPI server application for agno-harness."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from ..api_paths import HEALTH_PATH, join_prefix

if TYPE_CHECKING:
    from ..app import RelayApp
    from ..core.streamui.schema import CardCatalog
    from ..runtime.longrun import LongRunManager
    from ..runtime.runtime import AgentRuntime
    from ..transport.router import UserIdResolver

log = logging.getLogger("agno_harness.server")


class RelayServer(FastAPI):
    """Class-based FastAPI application for the agno-harness production gateway.

    Designed for clean object-oriented inheritance and customization.
    Subclass this to implement custom authentication, inject domain routes,
    add enterprise middleware, or customize webhook endpoints.

    Example:
    -------
    ```python
    from agno_harness.server import RelayServer
    from fastapi import Request


    class MyEnterpriseServer(RelayServer):
        def resolve_user_id(self, request: Request) -> str | None:
            # Custom auth logic (e.g. JWT header, Casdoor session)
            return request.headers.get("X-User-Id")

        def setup_routes(self) -> None:
            super().setup_routes()

            @self.get("/api/v1/health")
            async def health():
                return {"status": "ok", "service": "agno-harness"}


    server = MyEnterpriseServer(relay_app)
    # server.run(port=8000)
    ```
    """

    def __init__(
        self,
        relay: RelayApp | None = None,
        *,
        runtime: AgentRuntime | None = None,
        agent: Any | None = None,
        agent_or_runtime: Any | None = None,
        card_catalog: CardCatalog | None = None,
        title: str = "agno-harness Gateway",
        version: str = "0.2.0",
        api_prefix: str = "",
        allow_anonymous: bool = False,
        expose_debug_routes: bool = False,
        cors_origins: list[str] | None = None,
        resolve_user_id: UserIdResolver | None = None,
        long_runs: LongRunManager | None = None,
        **fastapi_kwargs: Any,
    ) -> None:
        if relay is not None:
            self.relay = relay
        elif runtime is not None:
            from ..app import RelayApp

            self.relay = RelayApp(runtime=runtime)
        elif agent is not None or agent_or_runtime is not None:
            from ..app import RelayApp
            from ..runtime.runtime import AgentRuntime

            target = agent if agent is not None else agent_or_runtime
            if isinstance(target, AgentRuntime):
                self.relay = RelayApp(runtime=target)
            else:
                rt = AgentRuntime(agent=target, catalog=card_catalog)
                self.relay = RelayApp(runtime=rt)
        else:
            msg = "Either 'relay', 'runtime', or 'agent' must be provided to RelayServer."
            raise ValueError(msg)

        self.api_prefix = api_prefix
        self.allow_anonymous = allow_anonymous
        self.expose_debug_routes = expose_debug_routes
        self.cors_origins = cors_origins if cors_origins is not None else ["*"]
        self._user_id_resolver = resolve_user_id
        self.long_runs = long_runs

        # Fail-fast validation on authentication in production
        if (
            not self.allow_anonymous
            and self._user_id_resolver is None
            and self.__class__.resolve_user_id is RelayServer.resolve_user_id
        ):
            from ..transport.router import ConfigurationError

            msg = (
                "RelayServer requires user authentication in production. Either:\n"
                "  1. Subclass RelayServer and override `resolve_user_id(self, request)`,\n"
                "  2. Pass `resolve_user_id=...` into RelayServer,\n"
                "  3. Explicitly pass `allow_anonymous=True` only for local development/testing."
            )
            raise ConfigurationError(msg)

        # Setup lifespan context manager
        @asynccontextmanager
        async def _default_lifespan(app: FastAPI) -> AsyncIterator[None]:
            await self.on_startup()
            try:
                yield
            finally:
                await self.on_shutdown()

        fastapi_kwargs.setdefault("lifespan", _default_lifespan)
        super().__init__(title=title, version=version, **fastapi_kwargs)

        self.setup_middleware()
        self.setup_routes()

    def resolve_user_id(self, request: Request) -> str | None:
        """Resolve caller user identity from the request.

        Override this method in subclasses to extract authenticated user info
        from headers, JWT tokens, session cookies, etc.
        """
        if self._user_id_resolver is not None:
            return self._user_id_resolver(request)
        return request.headers.get("X-User-Id")

    def setup_middleware(self) -> None:
        """Configure middleware stack. Override to add custom middleware."""
        if self.cors_origins:
            self.add_middleware(
                CORSMiddleware,
                allow_origins=self.cors_origins,
                allow_credentials=True,
                allow_methods=["*"],
                allow_headers=["*"],
            )

    def setup_routes(self) -> None:
        """Configure application routes.

        Automatically binds:
        - Health check endpoint at GET /api/v1/health
        - AG-UI wire protocol SSE & Thread routes via make_agui_router
        - Channel webhooks (e.g. Teams, Lark) if registered on relay.
        """
        from ..transport.router import make_agui_router

        if self.long_runs is None:
            resume_mode = "none"
        elif self.long_runs.can_follow_live:
            resume_mode = "live"
        else:
            resume_mode = "history"

        # 1. Standard Health Check
        @self.get(join_prefix(self.api_prefix, HEALTH_PATH), tags=["system"])
        async def health_check() -> dict[str, Any]:
            return {
                "status": "healthy",
                "title": self.title,
                "version": self.version,
                "channels": list(self.relay.channels.keys()),
                "resumeMode": resume_mode,
            }

        # 2. Mount AG-UI wire router
        agui_router = make_agui_router(
            runtime=self.relay.runtime,
            prefix=self.api_prefix,
            resolve_user_id=self.resolve_user_id,
            allow_anonymous=self.allow_anonymous,
            expose_debug_routes=self.expose_debug_routes,
            long_runs=self.long_runs,
        )
        self.include_router(agui_router)

        # 3. Mount Channel routers if available
        for name, (channel, _) in self.relay.channels.items():
            if hasattr(channel, "get_router"):
                channel_router = channel.get_router()
                self.include_router(channel_router)
                log.info(f"Mounted router for channel '{name}'")

    async def on_startup(self) -> None:
        """Lifecycle hook: runs when FastAPI server starts."""
        log.info(f"Starting {self.title} relay channels...")
        await self.relay.start()

    async def on_shutdown(self) -> None:
        """Lifecycle hook: runs when FastAPI server shuts down."""
        log.info(f"Stopping {self.title} relay channels...")
        await self.relay.stop()

    def run(self, host: str = "0.0.0.0", port: int = 8000, **uvicorn_kwargs: Any) -> None:
        """Run the server using uvicorn."""
        import uvicorn

        uvicorn.run(self, host=host, port=port, **uvicorn_kwargs)
