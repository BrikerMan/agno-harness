# 01. 五条铁律

协议不提供、产品自己做、不要假装底座有：消息编辑 / 重新生成、会话分叉、附件多模态、语音、虚拟列表、每 token 性能优化。

可运行的协议层是 [`frontend-kit/`](../../../../resources/frontend-kit/README_zh.md)。复制它。不要自己再写一遍 `applyEvent`。

## 五条

1. **一个 `applyEvent`，live 和 replay 共用。** 禁止从 Agno `GET /threads/{id}/messages` 重建再自己分组。
2. **只渲染 `body.order[]`。** slot 在元素**打开**时 push。工具出现在 markdown 下面 = 客户端重排了，不是 runtime 的错。
3. **历史走 `/frames`，不是 `/messages`。** `/messages` 是给模型的 lossy session。
4. **只有用户往上滚才脱离吸附。** 内容变高导致的 `scrollTop` 变不算脱离。
5. **Stop ≠ 断线。** 关 tab / 闪退 / 断网只是不看了。`POST /runs/{id}/abort` 只对应 Stop（以及产品明确「丢掉这次任务」）。

## 对照

| 错法 | 对的做法 |
| --- | --- |
| 用 `/messages` 重画气泡 | `/frames` + 同一套 `applyEvent` |
| 把 tool 从 `order[]` 捞出来追加到末尾 | 只按 `order[]` 画 |
| 关 tab 就 `POST .../abort` | 关 tab 不断任务；只有 Stop 才 abort |
| 每 token `scrollIntoView({ behavior: "smooth" })` | ResizeObserver + 用户上翻才 unpin |

下一步：[02 外壳](02-thread-shell.md)。
