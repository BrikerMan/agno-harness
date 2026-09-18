# Deep dive and FAQ

For people customizing the gateway: wire protocol, zero-change React, and production pitfalls.

1. **[01. Zero-change React integration and DevTools](01-react-zero-code-integration.md)** — drop-in upgrade for an existing AG-UI / SSE frontend, plus the debug panel.
2. **[02. AG-UI wire protocol](02-protocol-wire-spec.md)** — frame shapes and the state machine (`RUN_STARTED`, `TEXT`, `TOOL_CALL`, `CUSTOM run.paused`).
3. **[03. Production pitfalls and FAQ](03-production-pitfalls-and-faq.md)** — IM rate limits, auth, Dual Storage, JSONB, chime-in, common questions.
