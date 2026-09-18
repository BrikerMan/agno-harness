# 01. Five iron rules

The protocol does not give you these; the product owns them. Do not pretend the base has: message edit / regenerate, thread fork, multimodal attachments, voice, virtual lists, per-token animation.

## The five

1. **One `applyEvent` for live and replay.** Do not rebuild from Agno `GET /threads/{id}/messages` and regroup yourself.
2. **Render only `body.order[]`.** Push a slot when the element **opens**. Tools appearing under markdown means the client reordered — not a runtime bug.
3. **History is `/frames`, not `/messages`.** `/messages` is the lossy session for the model.
4. **Unpin only when the user scrolls up.** A `scrollTop` change caused by growing content is not an unpin.
5. **Stop ≠ disconnect.** Closing a tab / crash / network drop is just looking away. `POST /runs/{id}/abort` is only for Stop (or an explicit “drop this job”).

## Contrast

| Wrong | Right |
| --- | --- |
| Redraw bubbles from `/messages` | `/frames` + the same `applyEvent` |
| Pull tools out of `order[]` and append | Paint `order[]` only |
| Abort on tab close | Tab close does not abort; only Stop does |
| Per-token `scrollIntoView({ behavior: "smooth" })` | ResizeObserver; unpin only on user scroll-up |

Next: [02 Shell](02-thread-shell.md).
