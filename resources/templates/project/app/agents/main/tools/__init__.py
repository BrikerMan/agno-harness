"""Toolkits for the main agent.

Extend
------
Add app/agents/main/tools/<name>.py with one Toolkit.
Append an instance to TOOLS.
Tools return short structured data and call app.services.
Card classes live under cards/. Channel code lives under app/channels/.
"""

from app.agents.main.tools.clock import ClockTools

TOOLS = [ClockTools()]
