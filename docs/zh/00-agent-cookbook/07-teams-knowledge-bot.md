# 07. Markdown 笔记

`agno-harness init` 总会写上笔记、一个技能和一个占位帮手 `AgentBuilder`。`--channel` 只决定进程怎么启动。`AgentBuilder` 和 `app/identity.py` 里的解析都标了 `MUST CHANGE BEFORE PRODUCTION`。

种子笔记在 `app/knowledge/*.md`。进程启动时，缺的文件会复制到 `data/knowledge/`。Agent 搜索的是 `data/knowledge/`。数据库是 `data/agent.db` 和 `data/sessions.db`。`data/` 已在 `.gitignore` 里。改 `data/knowledge/` 里的文件，下一条消息就能看到。

```bash
agno-harness init . --channel cli
```

技能跟使用它的 Agent 放在一起：`app/agents/<name>/skills/<skill>/SKILL.md`。`cards:` 里的名字必须已经注册在这个 Agent 的 `CARD_CATALOG` 上。

加帮手：新建 `app/agents/<name>.py`，写出 `build()`，在主 Agent 的 `assemble()` 里传进去。帮手不挂渠道。Teams 进程用 `build_teams_resolver()`，其它渠道用 `build_user_resolver()`。第一个项目的完整目录在 [README](../../../README_zh.md)。
