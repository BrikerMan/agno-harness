# 测试

何时读：给 runtime / 模块写测试。不要在单测里打真模型。

```bash
uv add --dev pytest pytest-asyncio
```

`asyncio_mode = "auto"`。Agent 只要求 `arun(**kwargs)` 产出 Agno chunk 异步迭代器——用脚本化 fake：

```python
runtime = AguiRuntime(agent=FakeAgent([content("hi"), run_completed()]), sequencer_mode=SequencerMode.AUDIT)
events = [e async for e in runtime.stream_events(make_input())]
```

仓库里的 `tests/conftest.py` 已有 `FakeAgent` 和 chunk builder。测协议用 `SequencerMode.STRICT` / `AUDIT`。测 HITL 用 `run_paused` + trailing tool messages（四种 `pauseType`、resume 要落 `TOOL_CALL_RESULT`）。测长任务用 `LongRunManager` + `InMemoryRunEventLog`。paused 的 tail 必须结束，不能挂住。

集成测（真 LLM）用 pytest marker `integration`，默认不要跑。
