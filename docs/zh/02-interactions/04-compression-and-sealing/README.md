# 02-interactions / 压缩与封口

长对话里上下文会爆 Token，Agno 原生压缩还会卡流、击穿 KV Cache。`SmartCompressionManager` 用一次 In-Context Checkpoint 接管记忆；`seal_session_run` 补齐未闭合工具调用。

装配总览：[07 写一个 Agent](../../01-foundations/07-writing-an-agent.md)。

| 步 | 内容 |
| --- | --- |
| [01 为什么 + `num_history_runs`](01-why-and-num-history.md) | 原生缺陷；`0` / `None` / `100` |
| [02 检查点与配置](02-checkpoint-and-config.md) | 阈值矩阵、checkpoint、user 角色、接入模板 |
| [03 封口与 FAQ](03-seal-and-faq.md) | `seal_session_run`；SKIP / None FAQ；**Web 怎么确定没问题** |
