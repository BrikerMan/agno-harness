"""Toolkits for the researcher.

Extend
------
Add app/agents/researcher/tools/<name>.py with one Toolkit.
Append an instance to TOOLS.
"""

from app.agents.researcher.tools.exa import toolkit as exa

TOOLS = [exa]
