"""Web Demo for agno-relay.

Run directly:
    uv run python examples/02_web_demo.py

Then visit in browser:
    http://localhost:8000
    or API at http://localhost:8000/docs
"""

import os
from typing import Any
import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from agno.agent import Agent
from agno_relay import (
    AguiRuntime,
    BlockSchema,
    CardCatalog,
    ItemSchema,
    WebChannel,
)


# 1. Define Class-First Card
class StockItem(ItemSchema):
    schema_name = "stock"
    symbol: str
    target: float

    async def resolve(self, ctx: Any = None) -> dict[str, Any]:
        return {
            "name": f"{self.symbol} Inc.",
            "price": 182.50,
            "change": "+2.4%",
        }


class StockWatchBlock(BlockSchema):
    schema_name = "stock-watch"
    body = "items"
    item = StockItem
    title: str | None = None


catalog = CardCatalog([StockWatchBlock])


# 2. Build Agent & Runtime
class OfflineWebAgent:
    """Mock agent that streams AG-UI content for Web demo."""
    def __init__(self, instructions: str):
        self.instructions = instructions

    async def arun(self, *args, **kwargs):
        from agno.run.agent import RunContentEvent, RunCompletedEvent
        yield RunContentEvent(content="Here is your portfolio watchlist:\n\n")
        yield RunContentEvent(
            content=(
                "```stream-ui {\"schema\": \"stock-watch\", \"title\": \"Tech Watchlist\"}\n"
                "{\"symbol\": \"AAPL\", \"target\": 200.0}\n"
                "{\"symbol\": \"NVDA\", \"target\": 140.0}\n"
                "```\n\n"
                "Streaming completed successfully."
            )
        )
        yield RunCompletedEvent()


runtime = AguiRuntime(
    agent=OfflineWebAgent("You are a financial analyst bot."),
    catalog=catalog,
)

# 3. Mount WebChannel onto FastAPI
app = FastAPI(title="agno-relay Web Demo")

web_channel = WebChannel(runtime=runtime)
# Mount official AG-UI SSE protocol endpoints
app.include_router(web_channel.get_router(), prefix="/api")


# Simple HTML interactive page to test SSE in real-time
HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>agno-relay Web Demo</title>
  <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-slate-900 text-slate-100 min-h-screen p-8 flex flex-col items-center">
  <div class="max-w-2xl w-full">
    <h1 class="text-3xl font-bold mb-2 text-indigo-400">agno-relay WebChannel Demo</h1>
    <p class="text-slate-400 mb-6">Real-time Server-Sent Events (SSE) via AG-UI protocol with auto-reconnect.</p>
    
    <div class="bg-slate-800 rounded-lg p-4 mb-4 border border-slate-700">
      <div class="flex gap-2">
        <input id="promptInput" type="text" value="Show tech stocks" class="flex-1 bg-slate-900 border border-slate-700 rounded px-3 py-2 text-white focus:outline-none focus:border-indigo-500" />
        <button onclick="sendPrompt()" class="bg-indigo-600 hover:bg-indigo-500 text-white font-medium px-4 py-2 rounded transition">Send (Stream)</button>
      </div>
    </div>

    <div class="bg-slate-800 rounded-lg p-4 border border-slate-700 min-h-[300px]">
      <div class="text-xs uppercase font-semibold text-slate-500 mb-2">Live AG-UI Stream Output:</div>
      <pre id="output" class="font-mono text-sm whitespace-pre-wrap text-emerald-400"></pre>
    </div>
  </div>

  <script>
    async function sendPrompt() {
      const text = document.getElementById("promptInput").value;
      const out = document.getElementById("output");
      out.textContent = "Connecting to SSE stream...\\n";

      const payload = {
        thread_id: "demo-thread-" + Math.floor(Math.random() * 1000),
        run_id: "demo-run-" + Date.now(),
        state: {},
        messages: [{ id: "m1", role: "user", content: text }],
        tools: [],
        context: [],
        forwarded_props: null
      };

      const res = await fetch("/api/agui", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        out.textContent += decoder.decode(value);
      }
    }
  </script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def home():
    return HTML_PAGE


if __name__ == "__main__":
    uvicorn.run("02_web_demo:app", host="127.0.0.1", port=8000, reload=True)
