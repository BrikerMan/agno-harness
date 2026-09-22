"""Application package.

Layout
------
agent.py                 process entry
app/main.py              FastAPI composition root
app/cli.py               terminal entry
app/settings.py          model from AGNO_HARNESS_LLM_*
app/identity.py          build_user_resolver and build_teams_resolver
app/paths.py             data/ for databases and markdown notes
app/runtime_factory.py   RelayApp for the main agent only
app/knowledge/           seed notes, copied into data/knowledge/ when missing
app/agents/main/         coordinator: instructions, tools, cards, actions, skills
app/agents/agent_builder.py   placeholder helper. Replace before production.
app/channels/            one module per transport. mount_all calls the ones this process serves
app/services/            shared functions. No FastAPI and no channel SDK

Extend
------
New specialist: add app/agents/<name>.py with NAME and build(), then pass it to assemble() in app/agents/main/agent.py.
New channel: add app/channels/<name>.py with mount(relay, app), import it, and call it in mount_all.
An enabled channel with missing settings logs an error and raises.
"""
