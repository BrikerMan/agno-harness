# 前端 React 0 改动集成指南与 DevTools 调试面板

产品壳（铁律、侧栏、跟滚、attach）走 [Web / React 五步](../03-clients/01-web-react/README.md)。本篇只留：**已有 AG-UI 客户端可以零改动指向本服务**，以及 DevTools 面板合同。

许多下游业务团队已有基于标准 AG-UI / Server-Sent Events (SSE) 协议的前端对话界面。

升级到 `agno-harness` 时，**前端代码不需要进行任何破坏性重构，即可实现 100% 平滑兼容，且能够即刻获得更强的可观测性与调试能力。**

---

## 1. 为什么前端可以 0 代码改动？

`agno-harness` 内部的核心执行引擎 `AgentRuntime` 严格遵守标准 AG-UI 传输协议规范：

- **端点路径完全一致**：`POST /agui/runs`（或挂载自定义前缀）；
- **请求格式完全一致**：标准 `RunAgentInput`（包含 `thread_id`, `run_id`, `messages`, `state`, `context`）；
- **SSE 事件帧格式完全一致**：标准事件流（`event: run_started`, `event: text_message_content`, `event: custom`, `event: run_finished`）；
- **心跳保活完全兼容**：默认以 5 秒间隔下发 `: ping\n\n` 注释帧，ALB / Nginx 反向代理绝不断连。

因此，已经对接标准 AG-UI 的 React 前端，只需把请求的基础 URL 指到 `agno-harness` 所在端口，即可恢复对话与卡片渲染。

---

## 2. 深度功能增强：如何在前端消费新特性

虽然 0 改动即可运行，但 `agno-harness` 提供了数项强大的进阶扩展，前端只需几行代码即可点亮：

### A. 人工审批（HITL）交互
当后端工具需要人类确认时，前端会收到一条 `CUSTOM` 事件帧：
```json
{
  "event": "custom",
  "name": "run.paused",
  "value": {
    "action_id": "agno.hitl.resume",
    "tool_call_id": "call_abc123",
    "tool_name": "restart_production_service",
    "pause_type": "confirmation",
    "tool_args": {"service": "order-api"}
  }
}
```
**前端响应范式**：
在收到 `name === "run.paused"` 时弹窗或展示审批卡片；当用户点击“同意”后，前端向 `/agui/runs` 再次发起请求，带上 Resume 载荷即可无缝唤醒智能体：
```typescript
const resumePayload = {
  thread_id: currentThreadId,
  run_id: uuidv4(),
  tools: [],
  messages: [
    {
      id: uuidv4(),
      role: "tool",
      tool_call_id: "call_abc123",
      content: JSON.stringify({ accepted: true }) // 传回人类批准指令
    }
  ]
};
```

### B. 动作同步广播（`action.executed` / `action.resolved`）
当其他人在飞书群或 Teams 里点击了卡片审批按钮时，`agno-harness` 会向 Web 端的 Custom 事件流中广播：
```json
{
  "event": "custom",
  "name": "action.resolved",
  "value": {
    "tool_call_id": "call_abc123",
    "decision": "approved",
    "resolver": "Alice (via Lark)"
  }
}
```
Web 前端监听该事件即可自动关闭等待中的审批模态框，并变为“已被 Alice 在飞书端审批”，实现跨端实时状态互通！

---

## 3. 开箱即用 React Debug DevTools 面板

为了排查复杂会话、多轮上下文、Token 消耗以及流式断连问题，宿主挂一个消费 `frames` / `violations` 的 Debug 面板即可（合同见 [Web 05](../03-clients/01-web-react/05-hitl-cards-devtools.md)）：

- **组件一览**：
  - `DebugPanel.tsx`: 嵌入式调试悬浮窗抽屉；
  - `TimelineTab.tsx`: 毫秒级可视化时间轴，清晰查看从收到请求 -> LLM 首字耗时 (TTFT) -> 各工具调用时序；
  - `EventsTab.tsx`: SSE 原始帧捕获与 JSON 树形探查；
  - `ProtocolTab.tsx`: 检查协议合规性（检测是否有悬挂未闭合的 tool call，是否有乱序 frame）；
  - `ChunksTab.tsx`: 原始 chunk 分片回放。

### 引入方式
```tsx
import { DebugPanel } from './agui-devtools/DebugPanel';

function App() {
  return (
    <div className="relative">
      <ChatInterface />
      {/* 仅在开发环境或管理员状态下展示 */}
      {process.env.NODE_ENV === 'development' && (
        <DebugPanel />
      )}
    </div>
  );
}
```
