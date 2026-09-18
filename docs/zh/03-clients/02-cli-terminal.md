# 03-clients / CLI 终端与交互式控制台 (CLI Channel)

对于本地开发、命令行极客工具或运维脚本，`CLIChannel` 提供了高度优化的 Rich 终端交互体验。

---

## 1. 快速使用

```python
import asyncio
from agno.agent import Agent
from agno.models.openai import OpenAIChat
from agno_harness import RelayApp, CLIChannel, RelayConfig

async def main():
    agent = Agent(
        model=OpenAIChat(
            id=RelayConfig.llm_model(default="gpt-4o"),
            base_url=RelayConfig.llm_base_url() or None,
            api_key=RelayConfig.llm_api_key() or None,
        ),
        description="CLI 极客助手",
        telemetry=False,
    )
    
    cli = CLIChannel()
    relay = RelayApp(agent).add_channel(cli)
    
    await relay.start()

if __name__ == "__main__":
    asyncio.run(main())
```

---

## 2. 核心特性

- **Rich 格式支持**：自动支持 ANSI 语法高亮、Markdown 渲染、Emoji、彩色加粗；
- **打字机流式输出**：`CLIChannel` 默认配置为 `StreamMode.RAW`，文字如行云流水般即时输出；
- **自适应卡片降级**：当 Agent 产生结构化卡片时，CLI 渠道会优先调用卡片的 `render_cli(resolved)` 方法；若未实现，则自动格式化为整齐的 Rich Panel 边框面板；
- **内建会话命令**：用户在控制台直接输入 `/reset`、`/new` 即可清空上下文。
