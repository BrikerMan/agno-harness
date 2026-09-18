# 03-clients / CLI terminal and interactive console (CLI Channel)

For local development, command-line tools, or ops scripts, `CLIChannel` gives a Rich-optimized terminal.

---

## 1. Quick start

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
        description="CLI power-user assistant",
        telemetry=False,
    )

    cli = CLIChannel()
    relay = RelayApp(agent).add_channel(cli)

    await relay.start()

if __name__ == "__main__":
    asyncio.run(main())
```

---

## 2. What you get

- **Rich formatting**: ANSI highlighting, Markdown, emoji, color and bold
- **Typewriter stream**: `CLIChannel` defaults to `StreamMode.RAW`, so tokens land as they arrive
- **Adaptive card fallback**: when the agent emits a structured card, the CLI channel prefers `render_cli(resolved)`; if that is missing, it formats a tidy Rich Panel
- **Built-in session commands**: type `/reset` or `/new` in the console to clear context
