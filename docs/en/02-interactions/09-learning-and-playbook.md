# 09. Continuous learning and living playbooks

Rules, corrections, glossaries, and chat-room preferences taught by users should compound across runs instead of resetting with every session or bloating chat history with duplicated static prompts.

`agno_harness.learning` operates **strictly on a local directory `root_dir: Path`** (zero cloud SDK dependencies, zero database migrations). Remote storage mirroring and channel-specific group-chat fetching stay in the application layer via minimal hooks and plain-text digest inputs.

## 1. Three-layer local directory layout

```text
data/learning/
├── README.md                  # Declares owner_agent and read/write boundaries for sibling agents
├── playbook.md                # Layer 1: standing rules (hot-injected at messages[0] every turn)
├── mistakes.md                # Layer 1: newest-first corrections (hot-injected every turn)
├── CHANGELOG.md               # Human-readable global change log (newest first)
├── topics/                    # Layer 2: topic notes / glossaries (catalog injected; read/grep on demand)
│   ├── README.md
│   └── <slug>.md
├── scopes/                    # Layer 3: per-user and per-group preferences (RunContext-isolated)
│   ├── README.md
│   ├── users/<slug>-<hash>.md
│   └── groups/<slug>-<hash>.md
└── audit/                     # Immutable per-change snapshots, rollbacks, and digest cursors
    ├── README.md
    ├── changes/<ts>-<id>.json
    └── cursors/<key>.json
```

## 2. Live per-turn reload (zero history bloat)

Do not concatenate `playbook.md` once at startup, and do not copy it into every `<user-query>`. Use `LearningStore.compose_instructions(...)` to pass a `RunContext`-aware callable into `Agent(instructions=...)` so edits take effect on the very next turn at `messages[0]`:

```python
from agno.agent import Agent
from agno_harness import LearningStore, LearningToolkit

learning = LearningStore("data/learning", seed_dir="app/learning", owner_agent="coordinator")

agent = Agent(
    name="Coordinator",
    model=model(),
    instructions=learning.compose_instructions(BASE_INSTRUCTIONS),
    tools=[LearningToolkit(learning)],
)
```

`AgentRunner` automatically forwards `platform`, `chat_id`, `sender_id`, `sender_name`, and `is_direct_message` into `RunContext.metadata`. `LearningToolkit`'s file tools (`read`, `grep`, `list_files`, `write_file`, `patch_file`) hide and reject access to `scopes/` and `audit/` so preferences never leak across users or chats.

## 3. Audit snapshots, tombstones, and self-healing

- **One immutable file per change**: `audit/changes/<ts>-<id>.json` records `before_content`, `after_content`, `after_sha256`, `diff`, actor, and reason—safe under concurrent writers.
- **Tombstones over bare deletes**: Reverting a newly created file or clearing a preference writes `status: archived` frontmatter instead of only deleting the file, preventing bidirectional `rclone copy --update` syncs from resurrecting old files.
- **Corruption fallback and `doctor()`**: If an external writer truncates or corrupts a Markdown file, `read_text()` automatically falls back to the latest valid audit snapshot; `learning.doctor(repair=True)` (or the `repair_learning` tool) restores missing READMEs, repairs broken files, and reconciles or reverts external SHA-256 drift.

## 4. Upper-layer sync hooks and background digest

- **Sync hooks**: Pass optional `on_before_read(rel_path)` and `on_after_write(rel_path)` callbacks when mirroring the local folder to remote storage.
- **Session & group-chat digesting (`LearningDigestor`)**:
  - `attach_session_close_digest(relay.session_manager, LearningDigestor(learning, model()), sink)` reflects on closed/reset sessions asynchronously from the message sink.
  - Scheduled jobs reading group chats pass incremental transcripts into `await digestor.digest_transcript(text, source_key=..., cursor=..., chat_id=...)`; `LearningDigestor` defaults to `NOOP` unless durable learnings exist and persists watermarks under `audit/cursors/`.
