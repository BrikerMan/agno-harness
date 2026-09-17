# AGENTS.md: Developer & Coding Agent Guidelines

> This document provides strict engineering rules and context for AI coding agents (Cursor, Claude Code, OpenCode) and human contributors working inside the `agno-relay` codebase.

---

## 1. Architecture Guardrails

- **Zero Circular Imports**:
  - Never introduce global catalog instance decorators (e.g. `@catalog.renderer`).
  - Always implement cards as **Class-First Components** inheriting from `ItemSchema` or `BlockSchema`.
- **Strict Layering**:
  - `core/`: Pure protocols, models, and parsers. MUST NOT import `agno`, `fastapi`, or channel SDKs.
  - `runtime/`: Agno agent execution, sequencer, compression, and clean seal. MUST NOT import FastAPI.
  - `channels/`: Transport adapters (Web, Teams, Lark, CLI). Isolated from each other.
  - `sinks/`: Pluggable message audit sinks (SQLite, Postgres). Decoupled from runtime via async event listeners.
- **Fail-Fast & Strict Validation**:
  - Pydantic models must use `model_config = ConfigDict(extra="forbid")` unless explicitly handling external raw webhooks.
  - Use `uv` for package management. Never use `pip`, `poetry`, or `conda`.

---

## 2. Coding Patterns

### Card Schemas
- Always provide a `schema_name: ClassVar[str] = "..."`.
- Model parameters should be minimal (IDs, brief notes).
- Authoritative details belong in the async `resolve(self, ctx=None)` method.
- Teams Adaptive Card fragments belong in `render_teams(self, resolved)`.
- Lark Interactive Card fragments belong in `render_lark(self, resolved)`.

### Stream Modes
- Web / CLI: `stream_mode="raw"` (live unbuffered chunks).
- Teams / Lark: `stream_mode="final"` (recommended default) or `stream_mode="throttle"` (1.5s window). Never stream raw token chunks to Teams/Lark.

### Testing Standard
- Every new module in `src/agno_relay/` must have a corresponding test file in `tests/`.
- Mock external network calls (Teams Connector, Lark OpenSearch) in unit tests using deterministic fixtures.
- Run `make check` (format + lint + test) before completing any task.
