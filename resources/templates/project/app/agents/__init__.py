"""Agents.

Extend
------
The coordinator is app/agents/main/agent.py. It is the only agent on AgentRuntime.
A specialist with its own instructions and tools is app/agents/<name>/,
the same shape as main: agent.py, instructions.md, and tools/.
A specialist with no tools of its own can stay app/agents/<name>.py.
Pass build() into assemble() from the coordinator. A specialist is not given a channel.
"""
