# 任务规划与动态 Todo (Todo Workflow)

在复杂 Agent 场景中，多步骤任务需要向用户清晰展示当前执行进度与状态。`agno-relay` 提供了标准的**动态任务规划机制**（支持标题、单层/两层级子步骤切换、实时进度更新、解耦展示及智能闭环兜底）。

参考实现：
- 后端 Toolkit：[`src/agno_relay/tools/todo.py`](../src/agno_relay/tools/todo.py)
- 前端常驻侧栏/浮窗：[`examples/demo/frontend/src/components/todo/TodoPanel.tsx`](../examples/demo/frontend/src/components/todo/TodoPanel.tsx)
- 前端内联提示条：[`examples/demo/frontend/src/components/streamui/renderers.tsx`](../examples/demo/frontend/src/components/streamui/renderers.tsx)

---

## 1. 架构选型与传输协议：为什么由 Harness 发射 UI Event？

在动态 Todo 的设计中，曾面临两个选择：
1. **方案 A（前端硬解 ToolCall）**：前端直接监听原生 `tool_call_start` / `tool_call_args` 事件，读取 `todo_write` 的 arguments 参数字符串并自行解析。
2. **方案 B（Harness 发射结构化 UI Event）**：大模型发起标准 ToolCall，由后端 Harness（运行时）清洗校验后，发射标准的 `ui.block.start` / `ui.item` 结构化事件，前端仅作为受控视图消费 `uiBlocks`。

**我们坚定选择了方案 B（Harness 发射 UI Event）**，核心原因如下：

### 1.1 为什么选择 Harness 发射 UI Event？

| 考量维度 | 方案 A：前端硬解 ToolCall | 方案 B：Harness 发射 UI Event（本方案） |
| :--- | :--- | :--- |
| **职责边界** | 前后端均需编写复杂的 Markdown 正则与层级解析逻辑，容易发生双端不一致 | **单一真实数据源**：Harness 统一清洗校验，计算 parent/child 树状层级与状态枚举，前端零解析负担 |
| **Streaming 体验** | 在流式生成参数时，`arguments` 是未闭合的 JSON 片段（Partial JSON），前端需做繁琐的容错修复 | **结构化就绪**：Harness 在 tool 执行时流式 emit 清洗后的标准事件，前端开箱即用，无需解析未闭合文本 |
| **历史回放与持久化** | 回放时前端必须重走一遍 ToolCall 参数反序列化与正则重算，逻辑冗余 | **天然对齐**：事件日志库（`RunEventLog`）原样持久化 `CUSTOM` 帧，回放与实时流使用**完全相同的事件处理管道** |
| **体系一致性** | Todo 成为前端的一个特例硬编码逻辑 | **统一卡片规范**：与数据图表（`research-graph`）、卡片评审、代码评审等**共享统一的 StreamUI 协议** |

> **关键原则**：大模型端依然发起最标准的 Function Calling（调用 `todo_write` 工具），保持模型交互的标准性；而在框架底层，Harness 将工具的副作用提升为强类型的结构化 UI 表达。

---

### 1.2 实时流式（Live Streaming）的处理机制

当 Agent 运行并更新任务进度时，实时事件流转如下：

```text
[大模型] ──(发起 ToolCall)──> todo_write(todos="- [x] 1. 准备\n- [-] 2. 执行...")
                                       │
                                       ▼
                       [Harness: TodoToolkit 执行体]
                                       │ 1. parse_todo_list() 清洗为结构化 items
                                       │ 2. 计算 2 级树状缩进 (level=0 / level=1)
                                       ▼
                      [AG-UI Custom Events SSE 流]
                         ├─ ui.block.start (schema="todo-list", blockId="b-xxx")
                         ├─ ui.item        (data: { label: "1. 准备", state: "done", level: 0 })
                         ├─ ui.item        (data: { label: "2. 执行", state: "doing", level: 0 })
                         └─ ui.block.end   (blockId="b-xxx")
                                       │
                                       ▼
                     [前端: useAguiChat / applyEvent]
                         │
                         ├─ 命中 REPLACING_SCHEMAS 原地替换先前块 (In-place Replace)
                         └─ TodoPanel / TodoListRenderer 实时联动刷新进度条与打勾状态
```

1. **原子化流式更新**：
   - 每次任务变更，Harness 都会包裹在 `async with ui_block("todo-list"):` 中。
   - 先发送 `ui.block.start`，紧接着顺序 emit 每一项 `ui.item`，最后自动发出 `ui.block.end`。
2. **原地替换 (`REPLACING_SCHEMAS`)**：
   - Todo 列表是典型的**全量状态覆写型卡片**。
   - 前端 Reducer 将 `todo-list` 标记在 `REPLACING_SCHEMAS` 中。当收到相同 schema 的新 block 时，在消息内部**原地替换**已有块（`uiBlocks[previous] = newBlock`），而不是推入新块，**彻底防止任务列表随着多轮对话无限向下滚动刷屏**。
3. **实时动效与交互反馈**：
   - `doing` 状态项自动呈现旋转 Spinner 与高亮呼吸光晕。
   - `done` 状态项自动呈现平滑打勾动画并置灰。
   - 顶栏与面板上的进度百分比联动递增。

---

### 1.3 历史回放（Session Replay & Resume）的处理机制

在离线查看历史会话、刷新页面重连、或者多端查看历史记录时，UI Event 展现出巨大的优势：

```text
               ┌──────────────────────────────────────────────┐
               │    持久化存储 (SQLite / Redis RunEventLog)   │
               │   已完整归档包含 ui.block / ui.item 的事件序列 │
               └──────────────────────┬───────────────────────┘
                                      │
                         GET /runs/{id}/frames (重放事件流)
                                      │
                                      ▼
                        [前端 applyEvent 统一还原]
                  （live 实时流 与 replay 历史回放 100% 共用）
                                      │
                                      ▼
             [恢复最终精确状态：所有已完成项一目了然，无二次计算与格式漂移]
```

1. **极简的前端回放（遵循第一铁律：一个 `applyEvent`，live 和 replay 共用）**：
   - 前端不需要为“历史回放”写任何特殊的重新解析逻辑。
   - 无论是长任务 Resume，还是切 Thread 读取历史，接口返回的帧流中已经天然包含了 `ui.block.start` 和 `ui.item`。
   - 前端的 `useAguiChat` 仅仅按照常规事件流无脑重放一次，`activeTodoBlock` 即可直接恢复出与当时一模一样的任务清单与打勾进度。
2. **确定性状态定格（故障现场不丢失）**：
   - 如果某次运行因为模型报错、断网或用户手动中断（Abort）而停止，存储层记录的就是中断前的最后一个 `ui.item` 帧。
   - 重新打开会话回放时，界面如实展现中断时的现场（例如某一项卡在 `doing` 或 `error`），而不会因为前端“重新跑解析”而产生歧义或状态丢失。

---

### 1.4 层级支持与 `allow_subtasks`
- **默认不开启子步骤（`allow_subtasks=False`）**：单层扁平任务清单（`level = 0`）。大多数日常 Agent 仅需线性执行，单层列表避免模型过度拆解产生琐碎噪音。
- **显式开启两层树状子步骤（`allow_subtasks=True`）**：
  - 顶级步骤（Parent Task）：行首 `- [ ]`，`level = 0`。
  - 嵌套子步骤（Sub-task）：缩进 2 个空格或 Tab（`  - [ ]`），`level = 1`。

### 1.5 状态定义与 UI 映射
- `pending` (`- [ ]`)：未开始 / 排队中。
- `doing` (`- [-]`)：正在执行，前端展示动态旋转 Spinner 与高亮呼吸光晕。
- `done` (`- [x]`)：已完成，打勾并置灰划线。
- `error` (`- [e]`)：执行失败 / 异常中断。

---

## 2. 后端接入与继承

### 2.1 快速接入：使用 `TodoToolkit`（自动注入 Instruction）

在定义 Agno Agent 时，直接注册 `TodoToolkit`。**Toolkit 默认开启 `add_instructions=True`，Agno 会自动将针对该配置生成的 Prompt 注入 Agent 的系统提示词中，无需开发者手动修改主 Agent 的 instructions**：

```python
from agno.agent import Agent
from agno_relay import AguiRuntime, TodoToolkit

# 1. 初始化标准 Toolkit
# 默认为单层扁平任务清单（allow_subtasks=False）
todo_tool = TodoToolkit(default_title="任务执行规划")

# 如果需要支持 2 层级树形子步骤，显式传入 allow_subtasks=True：
# todo_tool = TodoToolkit(default_title="任务执行规划", allow_subtasks=True)

agent = Agent(
    name="workflow-agent",
    tools=[todo_tool],  # 自动注入对应形态的 todo instructions，主 instructions 保持专注业务
    instructions="你是一个专业的执行助手...",
)

runtime = AguiRuntime(agent=agent, catalog=catalog)
```

#### Toolkit 核心特性：
- **两类场景的独立 Prompt 自动生成与注入**：
  - **情况 1：`allow_subtasks=False`（默认单层扁平）**：
    自动注入纯单层清单 Prompt，引导模型输出清晰扁平的 `- [ ]` 列表，明确禁止缩进子步骤。
  - **情况 2：`allow_subtasks=True`（支持两级步骤）**：
    自动注入支持 2 空格缩进子步骤的树形 Prompt，引导模型将复杂阶段组织为 `- [-] 阶段` 嵌套 `  - [x] 子任务`。
  - **终态闭环铁律（两种情况均强制包含）**：
    Prompt 均硬性约束：**“在输出最终结论或结束回答前，必须最后一次调用 `todo_write` 将所有任务状态置为已完成 `[x]`，严禁残留 doing `[-]` 导致空转”**。
- **无需改动主 Agent**：通过 Agno 原生的 `Toolkit.add_instructions` 机制，Agent 在执行时自动合并 `agent._tool_instructions`，业务提示词与任务流提示词完美解耦。
- **精准的 Tool Description**：向大模型暴露的 `todo_write` 工具参数与描述会根据 `allow_subtasks` 自动适配，确保 Function Calling Schema 与系统提示词一致。
- **智能扁平化容错**：当 `allow_subtasks=False` 时，即使模型偶然在 Markdown 中缩进书写，解析器也会自动安全展平，防止破坏 UI 结构。
- **灵活的 instructions 访问方式**：
  - 属性或方法调用：`todo_tool.instructions` 或 `todo_tool.instructions()` 均可直接获取当前实例对应的完整 Prompt 字符串。
  - 类方法调用：`TodoToolkit.instructions(allow_subtasks=False)` 或 `TodoToolkit.instructions(allow_subtasks=True)`。

### 2.2 自定义继承与扩展

如果业务需要对任务状态增加自定义字段（如任务耗时预估、重试次数、分配执行人）：

#### 步骤一：扩展 `ItemSchema`
在你的卡片定义中继承 `ItemSchema`：

```python
from typing import Literal
from agno_relay.runtime.cards.schema import BlockSchema, ItemSchema

class CustomTodoItem(ItemSchema):
    schema_name = "todo"
    label: str
    state: Literal["pending", "doing", "done", "error"] = "pending"
    level: int = 0
    estimated_seconds: int | None = None
    assignee: str | None = None

class CustomTodoList(BlockSchema):
    schema_name = "todo-list"
    item = CustomTodoItem
    title: str | None = None
```

#### 步骤二：继承 `TodoToolkit`
重写或扩展 `TodoToolkit`，自定义卡片发射逻辑：

```python
from agno_relay import TodoToolkit, parse_todo_list
from agno_relay.runtime.modules.streamui import emit_item, ui_block

class ExtendedTodoToolkit(TodoToolkit):
    def __init__(self, service_url: str, allow_subtasks: bool = False) -> None:
        super().__init__(default_title="分布式任务流", allow_subtasks=allow_subtasks)
        self.service_url = service_url

    async def todo_write(self, todos: str, title: str | None = None) -> str:
        # 1. 继承解析能力
        items = parse_todo_list(todos, allow_subtasks=self.allow_subtasks)
        
        # 2. 注入业务扩展字段
        for item in items:
            item["assignee"] = "worker-pool-1"

        # 3. 发射自定义卡片
        async with ui_block("todo-list", title=title or self.default_title):
            for item in items:
                await emit_item("todo", item)

        return f"已同步 {len(items)} 项任务到控制台。"
```

---

## 3. 前端实现指南：与 Chat 聊天分开展示

在产品级 Agent 界面中，**最关键的设计原则：将 Todo 任务规划与 Chat 聊天流彻底分开展示**。

### 3.1 为什么要与 Chat 聊天流解耦？

1. **避免聊天内容被严重刷屏**：长流程 Agent 执行 10~20 个子任务时，若每次更新都在聊天区输出卡片，会产生海量无效滚屏，用户无法聚焦在关键对话回复与工具输出上。
2. **生命周期的本质不同**：单条 Chat 消息是**历史时序流（Timestamped Log）**，而 Todo 任务规划是**全局当前状态（Live Global State）**。把全局状态硬塞进时序气泡会导致旧消息里的 Todo 状态混乱或被顶出可视区。
3. **沉浸式关注进展**：用户在等待耗时操作时（如 Bash 跑脚本、分析数据），视线可以直接停留在专门的 Todo 区域，聊天区仅保留高价值的文字交流与轻量级提示。

---

### 3.2 两种解耦展示形态

根据应用场景和屏幕尺寸，Todo 推荐采用以下两种解耦形态（甚至支持在两者间一键切换）：

```text
形态 A：右侧常驻侧栏 (Persistent Sidebar)          形态 B：独立弹窗/浮层 (Dedicated Popup Modal)
┌──────────────┬──────────────┬──────────────┐      ┌──────────────┬───────────────────────────────┐
│              │              │ 右侧常驻面板  │      │              │ 主聊天流 (Chat)               │
│  左侧会话/导航│  中间主对话流 │ - 任务总览   │      │  左侧会话/导航│  ┌─────────────────────────┐  │
│  - 历史列表   │  - 问候交流   │ - 80% 进度条 │      │  - 历史列表   │  │ 任务进度: 3/5 [点击展开] │  │
│              │  - 工具卡片   │ - 树状任务树 │      │              │  └─────────────────────────┘  │
│              │  - 极简横条   │ - 实时脉冲   │      │              │ ┌───────────────────────────┐ │
│              │              │              │      │              │ │ 弹窗浮层 (Popup Dialog)    │ │
└──────────────┴──────────────┴──────────────┘      │              │ │ 全局遮罩 + 毛玻璃任务树   │ │
(适合桌面端大屏、IDE 插件、深度复杂 Agent 工作台)       │              │ └───────────────────────────┘ │
                                                    └──────────────┴───────────────────────────────┘
                                                    (适合窄屏、移动端、或需要极致极简界面的场景)
```

#### 形态 A：右侧常驻工作台（Sidebar Dock）
- 固定在主界面右侧（宽度通常 `w-72` ~ `w-80`），与主对话流平级并列。
- 用户随时用余光监控进度，无需任何点击操作。
- 适合桌面 IDE、生产力平台、长时间运行的数据处理或多 Agent 协作工作台。

#### 形态 B：专门的浮窗 / 弹窗（Dedicated Popup Modal）
- 聊天流中仅保留一行极简卡片横条（如：`TodoListRenderer` 仅渲染 `Tasks: 3/5 (60%) 正在执行...`）。
- 点击该横条或顶栏徽章时，以对话框（Dialog / Modal）或浮层 Popover 形式居中弹出。
- 弹窗内包含完整的进度条、Done/Doing/Pending 统计与带缩进的 2 层级子任务列表。
- 支持点击遮罩或 `Esc` 键随时收起，不占用固定的屏幕横向宽度。

---

### 3.3 核心实现代码

#### 1. 主页面提取 Active Todo 并挂载面板
在主页面容器（如 `App.tsx`）中从最新消息提取激活的 `todo-list` 块，并挂载 `TodoPanel`：

```tsx
// 从最近的消息倒序查找当前激活的 todo-list block
const activeTodoBlock = useMemo(() => {
  for (let i = chat.messages.length - 1; i >= 0; i--) {
    const msg = chat.messages[i];
    const block = msg.uiBlocks?.find((b) => b.schema === "todo-list");
    if (block && block.items.length > 0) return block;
  }
  return null;
}, [chat.messages]);

return (
  <div className="flex h-screen overflow-hidden">
    {/* 左侧会话列表 */}
    <aside className="w-64 border-r">...</aside>

    {/* 中间主聊天区 */}
    <main className="flex-1 flex flex-col">
      {/* 顶栏可放置一键呼出待办按钮 */}
      {activeTodoBlock && (
        <button onClick={() => setTodoOpen(true)} className="px-2 py-1 text-xs">
          Tasks: {stats.done}/{stats.total}
        </button>
      )}

      <MessageList messages={chat.messages} />
      <Composer />
    </main>

    {/* 独立 Todo 面板：支持 sidebar 侧栏与 popup 弹窗一键无缝切换 */}
    <TodoPanel
      block={activeTodoBlock}
      open={todoOpen && activeTodoBlock !== null}
      onClose={() => setTodoOpen(false)}
      isStreaming={chat.isStreaming}
      mode="sidebar" // 可设为 "sidebar" 或 "popup"
    />
  </div>
);
```

#### 2. TodoPanel 支持侧栏与弹窗双模式
在 `TodoPanel.tsx` 中，通过容器层切换即可轻松支持两种形态：

```tsx
export function TodoPanel({ block, open, onClose, isStreaming, mode = "sidebar" }: TodoPanelProps) {
  const [displayMode, setDisplayMode] = useState<"sidebar" | "popup">(mode);
  if (!open || !block) return null;

  const content = (
    <div className="flex h-full flex-col">
      {/* 头部：标题、进度与形态切换按钮 */}
      <div className="flex items-center justify-between border-b p-3">
        <h3 className="font-semibold text-xs">{title}</h3>
        <div className="flex items-center gap-1">
          {/* 一键切换 Sidebar 与 Popup */}
          <button onClick={() => setDisplayMode(m => m === "sidebar" ? "popup" : "sidebar")}>
            {displayMode === "sidebar" ? <ExternalLink /> : <Sidebar />}
          </button>
          <button onClick={onClose}><X /></button>
        </div>
      </div>

      {/* 进度条与指标 */}
      <ProgressBar percent={stats.percent} />

      {/* 任务清单列表 */}
      <div className="flex-1 overflow-y-auto p-3 space-y-2">...</div>
    </div>
  );

  // 模式 B：居中浮层弹窗
  if (displayMode === "popup") {
    return (
      <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm p-4" onClick={onClose}>
        <div className="relative w-full max-w-lg max-h-[85vh] rounded-xl border bg-background shadow-2xl overflow-hidden" onClick={e => e.stopPropagation()}>
          {content}
        </div>
      </div>
    );
  }

  // 模式 A：右侧常驻侧栏
  return (
    <aside className="w-80 shrink-0 border-l bg-background flex flex-col">
      {content}
    </aside>
  );
}
```

---

### 3.4 关键逻辑：防空转闭环与中途停止处理

大模型在执行多步骤工作流时，存在两种典型的边界状态：
1. **边界 A（全流程已顺利跑完，但漏掉了最后一步打勾）**：在正文中输出了详尽的最终结论，却漏掉了最后一次调用 `todo_write` 把末尾项从 `[-]` 改成 `[x]`，导致进度卡在 90% 悬挂。
2. **边界 B（中途停止 / 异常中断 / 用户按 Stop）**：例如总共规划了 5 个步骤，执行到步骤 3 时被用户主动点 Stop 中止，或者遇到网络错误断流。

**系统采用精确的「Prompt 铁律约束 + 严格末步兜底 + 沉静中途停止态」分流处理**：

#### 1. 终态闭环铁律（Prompt 约束，针对正常执行）
在 `TodoToolkit.instructions` 中强制注入系统提示词硬约束：
> *"CRITICAL RULE: When finishing your workflow, you MUST call `todo_write` one final time to mark ALL tasks as completed `- [x]` BEFORE writing your final concluding text. Never leave tasks hanging in doing `[-]`."*

#### 2. 严格末步兜底（仅限“只差最后一步”的情况）
**重要边界**：绝不能自作聪明地把所有停在 doing 的任务都强行闭环！
前端仅当**严格同时满足以下三项条件**时，才将末尾项兜底标记为 `done`：
- 整轮对话流已彻底结束（`!isStreaming`）；
- 前面 0 到 N-2 项已**全部为 `done`**（没有任何未开始的 `pending` 或报错的 `error` 项）；
- 唯独末尾最后一项（`parsed[lastIndex]`）停留在 `doing` 或 `pending`（模型直接给出了最终总结文字，未调用工具打勾）。

```tsx
// 在 TodoPanel.tsx 中：
if (!isStreaming && parsed.length > 0) {
  const lastIndex = parsed.length - 1;
  const lastItem = parsed[lastIndex];
  const allPriorDone = parsed.slice(0, lastIndex).every((it) => it.state === "done");

  // 严格只差最后一步（doing 或 pending）时，自动闭环为 100% Done
  if ((lastItem.state === "doing" || lastItem.state === "pending") && allPriorDone) {
    return parsed.map((item, idx) =>
      idx === lastIndex
        ? { ...item, state: "done" as const, detail: item.detail || "Completed with final summary" }
        : item,
    );
  }
}
```

#### 3. 中途停止态处理（例如踩到步骤 3 就停了）
如果任务中途停止（例如还有 `pending` 项未执行，或者用户点 Stop 中断）：
1. **不要全部标记完成**：保留真实执行现场（例如 5 步中完成 2 步，进度条定格在 40%），禁止伪造完成状态。
2. **进行状态（`doing`）变成“已停止（Stopped）”，去除 loading 动画，整体呈现沉静灰色**：
   - **禁止旋转 Spinner**：不显示 `animate-spin` 的 `Loader2`，避免用户误以为后台仍在运行；
   - **静止灰色图标**：改用静态 `PauseCircle`，色彩为 `text-muted-foreground`；
   - **中性灰卡片样式**：边框与底色切换为 `border-border/60 bg-muted/20 text-muted-foreground`；
   - **Badge 标签**：显示灰色徽章 `Stopped`（`bg-muted text-muted-foreground border-border`）；
   - **进度条定格**：进度条停留在实际完成比例（如 40%），颜色从活跃的流式渐变蓝变为沉静的灰色条（`bg-muted-foreground/40`）。

---

### 3.5 子步骤序号层级算法与智能去重（Hierarchical Indexing）

在有子步骤时，**切忌直接用数组全局下标 `idx + 1`**（会导致第三个父任务因为前面插入了子任务而变成 `#4` 甚至 `#7`，造成断层跳号）。同时，模型输出的 Markdown 文本往往已经自带有 `1. ` 或 `2.1 `，若直接拼前缀会造成 `[#1] 1. 任务名` 的双重冗余。

**前端标准层级计算算法**：

```tsx
let parentNum = 0;
let subNum = 0;
let deepSubNum = 0;

const parsed = block.items.map((item, idx) => {
  const data = (item.data as Record<string, unknown>) ?? {};
  const rawLabel = String(data.label ?? "").trim();
  const level = Number(data.level ?? 0);

  // 检测模型是否已在文本中输出了序号（如 "1. ", "2.1 ", "3.2: "）
  const numMatch = rawLabel.match(/^(\d+(?:\.\d+)*)[.)\-:\s]\s*(.*)$/);

  let displayIndex = "";
  let label = rawLabel;

  if (level === 0) {
    parentNum += 1;
    subNum = 0;
    deepSubNum = 0;
    displayIndex = numMatch ? numMatch[1] : String(parentNum);
    label = numMatch ? (numMatch[2] || rawLabel) : rawLabel;
  } else if (level === 1) {
    subNum += 1;
    deepSubNum = 0;
    displayIndex = numMatch ? numMatch[1] : `${parentNum}.${subNum}`;
    label = numMatch ? (numMatch[2] || rawLabel) : rawLabel;
  } else {
    deepSubNum += 1;
    displayIndex = numMatch ? numMatch[1] : `${parentNum}.${subNum}.${deepSubNum}`;
    label = numMatch ? (numMatch[2] || rawLabel) : rawLabel;
  }

  return {
    id: `${block.blockId}-${item.index ?? idx}`,
    displayIndex,
    state: data.state ?? "pending",
    label,
    level,
  };
});
```

渲染展示：
- 顶级任务（`level === 0`）：展示 `#{item.displayIndex}`（如 `#1`、`#2`、`#3`、`#4`）；
- 嵌套子步骤（`level === 1`）：展示 `↳ {item.displayIndex}`（如 `↳ 2.1`、`↳ 3.1`、`↳ 3.2`）；
- 正文标签自动剥离已匹配的前缀，视觉干净统一。

---

## 4. 常见问题排查与避坑指南

1. **为什么对话结束后还会卡顿几秒？**
   - 检查是否在 `runtime.on_post_run` 中挂载了同步阻塞的起名 Hook（`make_thread_title_hook`）。该 Hook 会在当前 SSE 连接中再次请求一次 LLM 生成标题。
   - 优化：在生产环境中，标题生成应完全异步离线运行（或者在前端首轮以首句截断快速显示），不要阻塞主 SSE 流关闭。
2. **为什么最后一步进度条卡在 80%~90% 一直在转？**
   - 模型在写完最终回复后未调用 `todo_write` 将所有任务置为已完成；
   - 检查是否挂载了 `todo_tool.instructions()`，且前端是否实现了第 3.4 节的智能闭环兜底逻辑。
3. **为什么聊天气泡里出现了长篇大段的 `todo_write` 原始参数卡片？**
   - Todo 是面板型编排工具，不应在消息气泡中重复渲染 raw tool call；
   - 做法：服务端挂载 `HideToolFilter({"todo_write"})`，或客户端在 `MessageBubble` 的 `BodyView` 渲染 `case "tool"` 时对 `call.name === "todo_write"` 直接返回 `null`。
4. **为什么子任务出现后，父任务序号错乱（如第 3 个任务变成 `#4`）？**
   - 不得使用数组遍历下标 `idx + 1`；必须使用第 3.5 节提供的树状 `parentNum` / `subNum` 层级算法。
