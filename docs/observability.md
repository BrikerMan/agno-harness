# 可观测性

何时读：要把一次回答变成一条 OTLP trace（Langfuse / Phoenix / 任意 collector）。

```bash
uv add "agno-relay[otel]"
```

两套互不重复：

- **`openinference-instrumentation-agno`**（`setup_otlp()` 装上）— 模型、工具、token。
- **toolbox `ObservabilityModule`** — 一次 AG-UI run 一根 span：嵌套模型 span、用户可见的 input/output、TTFT、帧数、客户端是否还在。不重记 prompt/工具。

```python
from agno_relay import ObservabilityModule, setup_otlp

setup_otlp()  # 进程启动时一次
runtime.register_module(ObservabilityModule().bind_agent(agent))
```

没配 collector 就是 no-op。这和 `BETTER_AGNO_TRACE_DIR` 本地 JSONL（调 parser 用）不是一回事。
