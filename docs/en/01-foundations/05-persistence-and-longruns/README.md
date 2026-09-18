# 01-foundations / Persistence and long runs

The Agno session is for the **next model turn**, not the stream the user saw. If a refresh must still show cards / child panels, or a run must outlive that HTTP request, use two stores plus `LongRunManager`.

Frontend idle reconnect → [Web 04](../../03-clients/01-web-react/04-attach-and-longrun.md). This series is routes and storage duties only.

| Step | Contents |
| --- | --- |
| [01 Dual storage](01-dual-storage.md) | Hot log vs archive; mixins; `X-Agui-Resume` |
| [02 Long-run routes](02-longrun-routes.md) | `LongRunManager`, `/attach`, `/active`, `/abort`; ping vs Redis beat |
| [03 FAQ](03-faq.md) | Missing prompt, blank bubble, Stop, `runCount` |
