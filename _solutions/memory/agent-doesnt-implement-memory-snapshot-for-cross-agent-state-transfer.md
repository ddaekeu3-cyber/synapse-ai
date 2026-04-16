---
layout: solution
title: "Agent Doesn't Implement Memory Snapshot for Cross-Agent State Transfer"
category: memory
description: "Capture complete agent memory and context state as a portable snapshot, then restore it in another agent for seamless multi-agent handoffs and delegation."
tags: [memory, multi-agent, handoff, state-transfer, snapshot, persistence]
---

When one agent hands off a task to another — whether due to specialization, load balancing, or orchestration — the receiving agent starts with no context. It re-reads the same documents, re-asks the same clarifying questions, and wastes tokens rediscovering what the first agent already knew. A memory snapshot captures accumulated state portably so the second agent picks up exactly where the first left off.

## Option 1: JSON Serialization Snapshot

Serialize the full conversation history, tool results, and extracted facts into a compact JSON snapshot. The receiving agent deserializes and injects the snapshot as a structured context block.

```python
import anthropic
import json
import time
from dataclasses import dataclass, asdict, field
from typing import Any

@dataclass
class MemorySnapshot:
    snapshot_id: str
    created_at: float
    agent_id: str
    conversation_history: list[dict]
    extracted_facts: dict[str, Any]
    tool_results: list[dict]
    task_state: dict[str, Any]
    metadata: dict[str, Any] = field(default_factory=dict)

def capture_snapshot(
    agent_id: str,
    conversation_history: list[dict],
    extracted_facts: dict,
    tool_results: list[dict],
    task_state: dict,
) -> MemorySnapshot:
    snapshot_id = f"snap_{agent_id}_{int(time.time())}"
    return MemorySnapshot(
        snapshot_id=snapshot_id,
        created_at=time.time(),
        agent_id=agent_id,
        conversation_history=conversation_history,
        extracted_facts=extracted_facts,
        tool_results=tool_results,
        task_state=task_state,
        metadata={"version": "1.0", "format": "json_v1"},
    )

def save_snapshot(snapshot: MemorySnapshot, path: str) -> None:
    with open(path, "w") as f:
        json.dump(asdict(snapshot), f, indent=2)

def load_snapshot(path: str) -> MemorySnapshot:
    with open(path) as f:
        data = json.load(f)
    return MemorySnapshot(**data)

def build_handoff_context(snapshot: MemorySnapshot) -> str:
    facts_text = "\n".join(f"- {k}: {v}" for k, v in snapshot.extracted_facts.items())
    tools_text = "\n".join(
        f"- {r['tool']}: {r['summary']}" for r in snapshot.tool_results[-5:]
    )
    return f"""## Handoff Context from Agent {snapshot.agent_id}

### Accumulated Facts
{facts_text}

### Recent Tool Results
{tools_text}

### Task State
{json.dumps(snapshot.task_state, indent=2)}

### Conversation Summary
Previous agent processed {len(snapshot.conversation_history)} turns.
Resume from this point without re-asking established facts.
"""

def run_agent_with_snapshot(snapshot: MemorySnapshot, new_task: str) -> str:
    client = anthropic.Anthropic()
    handoff_context = build_handoff_context(snapshot)

    messages = [
        {
            "role": "user",
            "content": f"{handoff_context}\n\nContinue with: {new_task}",
        }
    ]

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        system="You are a specialist agent receiving a handoff. Use the provided context to continue seamlessly.",
        messages=messages,
    )
    return response.content[0].text

# Demo
if __name__ == "__main__":
    # Agent A accumulates state
    snapshot_a = capture_snapshot(
        agent_id="agent_a",
        conversation_history=[
            {"role": "user", "content": "Analyze our Q4 sales data"},
            {"role": "assistant", "content": "I found 3 key trends..."},
        ],
        extracted_facts={
            "total_revenue": "$2.4M",
            "top_product": "Widget Pro",
            "growth_rate": "12%",
        },
        tool_results=[
            {"tool": "database_query", "summary": "Retrieved 1,247 transaction records"},
            {"tool": "chart_generator", "summary": "Created revenue trend chart"},
        ],
        task_state={"phase": "analysis_complete", "next_step": "generate_report"},
    )
    save_snapshot(snapshot_a, "/tmp/agent_a_snapshot.json")

    # Agent B receives handoff
    loaded = load_snapshot("/tmp/agent_a_snapshot.json")
    result = run_agent_with_snapshot(loaded, "Generate the executive summary report")
    print(result)

# Expected Token Savings: ~40-60% on handoff tasks — receiving agent skips re-discovery
# Environment: pip install anthropic
```

## Option 2: SQLite-Backed Snapshot Store

Persist snapshots to SQLite for durability, queryability, and multi-agent coordination. Agents can query the store by agent ID, task ID, or recency, enabling orchestrators to route tasks to agents with relevant accumulated context.

```python
import anthropic
import sqlite3
import json
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Optional

@contextmanager
def get_db(db_path: str):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

def init_snapshot_db(db_path: str) -> None:
    with get_db(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS snapshots (
                snapshot_id TEXT PRIMARY KEY,
                agent_id TEXT NOT NULL,
                task_id TEXT,
                created_at REAL NOT NULL,
                conversation_json TEXT NOT NULL,
                facts_json TEXT NOT NULL,
                state_json TEXT NOT NULL,
                tags TEXT DEFAULT ''
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_agent ON snapshots(agent_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_task ON snapshots(task_id)")

def save_to_db(
    db_path: str,
    agent_id: str,
    task_id: str,
    conversation: list[dict],
    facts: dict,
    state: dict,
    tags: list[str] = None,
) -> str:
    snapshot_id = str(uuid.uuid4())
    with get_db(db_path) as conn:
        conn.execute(
            """INSERT INTO snapshots
               (snapshot_id, agent_id, task_id, created_at, conversation_json, facts_json, state_json, tags)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                snapshot_id,
                agent_id,
                task_id,
                time.time(),
                json.dumps(conversation),
                json.dumps(facts),
                json.dumps(state),
                ",".join(tags or []),
            ),
        )
    return snapshot_id

def load_latest_snapshot(db_path: str, task_id: str) -> Optional[dict]:
    with get_db(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM snapshots WHERE task_id = ? ORDER BY created_at DESC LIMIT 1",
            (task_id,),
        ).fetchone()
    if not row:
        return None
    return {
        "snapshot_id": row["snapshot_id"],
        "agent_id": row["agent_id"],
        "conversation": json.loads(row["conversation_json"]),
        "facts": json.loads(row["facts_json"]),
        "state": json.loads(row["state_json"]),
        "created_at": row["created_at"],
    }

def handoff_via_db(db_path: str, task_id: str, new_instruction: str) -> str:
    client = anthropic.Anthropic()
    snapshot = load_latest_snapshot(db_path, task_id)

    if not snapshot:
        system = "You are starting a fresh task."
        messages = [{"role": "user", "content": new_instruction}]
    else:
        age_mins = (time.time() - snapshot["created_at"]) / 60
        context_block = f"""## Restored Context (from {snapshot['agent_id']}, {age_mins:.1f}m ago)

Facts established:
{json.dumps(snapshot['facts'], indent=2)}

Task state:
{json.dumps(snapshot['state'], indent=2)}

Prior conversation had {len(snapshot['conversation'])} turns.
"""
        system = "You are resuming a task via memory snapshot. Trust established facts; do not re-verify."
        messages = [{"role": "user", "content": f"{context_block}\n\n{new_instruction}"}]

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        system=system,
        messages=messages,
    )
    return response.content[0].text

# Demo
if __name__ == "__main__":
    DB = "/tmp/snapshots.db"
    init_snapshot_db(DB)
    TASK = "task_quarterly_report_2024"

    # Agent A saves snapshot after analysis phase
    save_to_db(
        DB, "agent_analyst", TASK,
        conversation=[{"role": "user", "content": "Analyze competitors"}, {"role": "assistant", "content": "Found 3 main competitors..."}],
        facts={"competitors": ["AlphaCo", "BetaInc", "GammaCorp"], "market_share": "our 34%, others 22%, 18%, 26%"},
        state={"phase": "competitive_analysis_done", "pending": "strategic_recommendations"},
        tags=["analysis", "q4", "competitive"],
    )

    # Agent B picks up the handoff
    result = handoff_via_db(DB, TASK, "Generate strategic recommendations based on the competitive analysis")
    print(result)

# Expected Token Savings: ~50% — eliminates re-analysis in multi-step pipelines
# Environment: pip install anthropic; uses stdlib sqlite3
```

## Option 3: Async Snapshot with Compression

For large conversation histories, compress snapshots with zlib before storage and decompress on load. An async background worker periodically auto-captures snapshots during long-running agent sessions, ensuring recovery points exist without blocking the main execution loop.

```python
import anthropic
import asyncio
import json
import time
import zlib
import base64
from dataclasses import dataclass, asdict
from typing import Any

@dataclass
class CompressedSnapshot:
    snapshot_id: str
    agent_id: str
    created_at: float
    compressed_data: str  # base64-encoded zlib
    original_size: int
    compressed_size: int

def compress_state(state: dict) -> tuple[str, int, int]:
    raw = json.dumps(state).encode()
    compressed = zlib.compress(raw, level=9)
    encoded = base64.b64encode(compressed).decode()
    return encoded, len(raw), len(compressed)

def decompress_state(encoded: str) -> dict:
    compressed = base64.b64decode(encoded)
    raw = zlib.decompress(compressed)
    return json.loads(raw)

async def auto_snapshot_worker(
    agent_id: str,
    state_getter,
    snapshot_store: dict,
    interval_seconds: float = 30.0,
    stop_event: asyncio.Event = None,
) -> None:
    """Background worker that captures snapshots at regular intervals."""
    if stop_event is None:
        stop_event = asyncio.Event()
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except asyncio.TimeoutError:
            pass
        if stop_event.is_set():
            break
        # Capture snapshot
        current_state = await state_getter()
        encoded, orig, comp = compress_state(current_state)
        snap_id = f"{agent_id}_{int(time.time())}"
        snapshot_store[snap_id] = CompressedSnapshot(
            snapshot_id=snap_id,
            agent_id=agent_id,
            created_at=time.time(),
            compressed_data=encoded,
            original_size=orig,
            compressed_size=comp,
        )
        ratio = (1 - comp / orig) * 100 if orig > 0 else 0
        print(f"[AutoSnapshot] {snap_id}: {orig}B → {comp}B ({ratio:.1f}% reduction)")

def get_latest_snapshot(store: dict, agent_id: str) -> CompressedSnapshot | None:
    agent_snaps = [s for s in store.values() if s.agent_id == agent_id]
    if not agent_snaps:
        return None
    return max(agent_snaps, key=lambda s: s.created_at)

async def run_agent_session_with_autosnapshot(task: str) -> str:
    client = anthropic.AsyncAnthropic()
    snapshot_store = {}
    conversation: list[dict] = []
    facts: dict = {}
    stop_event = asyncio.Event()

    async def get_state():
        return {
            "conversation": conversation[-20:],  # last 20 turns
            "facts": facts,
            "task": task,
            "turn_count": len(conversation),
        }

    # Start background auto-snapshot worker
    worker_task = asyncio.create_task(
        auto_snapshot_worker("agent_main", get_state, snapshot_store, interval_seconds=5.0, stop_event=stop_event)
    )

    # Simulate agent work
    messages = [{"role": "user", "content": task}]
    for i in range(3):
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=messages,
        )
        reply = response.content[0].text
        conversation.extend([
            {"role": "user", "content": task if i == 0 else f"Continue step {i+1}"},
            {"role": "assistant", "content": reply},
        ])
        facts[f"step_{i+1}_result"] = reply[:100]
        await asyncio.sleep(0)  # yield to snapshot worker

    stop_event.set()
    await worker_task

    # Transfer to specialist agent via snapshot
    latest = get_latest_snapshot(snapshot_store, "agent_main")
    if latest:
        restored = decompress_state(latest.compressed_data)
        handoff_ctx = f"Restored {restored['turn_count']} turns of context. Facts: {json.dumps(restored['facts'])}"
        specialist_messages = [{"role": "user", "content": f"{handoff_ctx}\n\nFinalize: {task}"}]
        final = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            messages=specialist_messages,
        )
        return final.content[0].text
    return "No snapshot available"

if __name__ == "__main__":
    result = asyncio.run(run_agent_session_with_autosnapshot("Research and summarize microservices best practices"))
    print(result)

# Expected Token Savings: ~35% storage overhead reduction; enables large context handoffs
# Environment: pip install anthropic
```

## Option 4: Versioned Snapshot with Diff-Based Sync

Track snapshots as an immutable version chain. Instead of transferring full state on every handoff, compute a diff between the last shared snapshot and the current state, sending only the delta. Receiving agents apply the diff to their local snapshot copy, minimizing transfer size for frequent collaborating agents.

```python
import anthropic
import json
import time
import hashlib
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Optional

@dataclass
class SnapshotVersion:
    version_id: str
    parent_id: Optional[str]
    agent_id: str
    timestamp: float
    full_state: dict
    diff_from_parent: Optional[dict] = None

def compute_diff(old_state: dict, new_state: dict) -> dict:
    """Compute minimal delta between two state dicts."""
    diff = {"added": {}, "modified": {}, "removed": []}
    all_keys = set(old_state) | set(new_state)
    for key in all_keys:
        if key not in old_state:
            diff["added"][key] = new_state[key]
        elif key not in new_state:
            diff["removed"].append(key)
        elif old_state[key] != new_state[key]:
            diff["modified"][key] = new_state[key]
    return diff

def apply_diff(base_state: dict, diff: dict) -> dict:
    result = deepcopy(base_state)
    result.update(diff.get("added", {}))
    result.update(diff.get("modified", {}))
    for key in diff.get("removed", []):
        result.pop(key, None)
    return result

def state_hash(state: dict) -> str:
    return hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()[:12]

class VersionedSnapshotStore:
    def __init__(self):
        self.versions: dict[str, SnapshotVersion] = {}
        self.agent_heads: dict[str, str] = {}  # agent_id -> latest version_id

    def commit(self, agent_id: str, state: dict) -> str:
        parent_id = self.agent_heads.get(agent_id)
        version_id = f"v_{agent_id}_{state_hash(state)}"

        diff = None
        if parent_id and parent_id in self.versions:
            diff = compute_diff(self.versions[parent_id].full_state, state)

        self.versions[version_id] = SnapshotVersion(
            version_id=version_id,
            parent_id=parent_id,
            agent_id=agent_id,
            timestamp=time.time(),
            full_state=state,
            diff_from_parent=diff,
        )
        self.agent_heads[agent_id] = version_id

        diff_keys = len(diff["added"]) + len(diff["modified"]) + len(diff["removed"]) if diff else "N/A"
        print(f"[VersionStore] Committed {version_id} (diff keys: {diff_keys})")
        return version_id

    def get_diff_for_handoff(self, sender_id: str, receiver_id: str) -> dict:
        """Get only the delta since receiver's last known state."""
        sender_head = self.agent_heads.get(sender_id)
        receiver_head = self.agent_heads.get(receiver_id)

        if not sender_head:
            return {}
        sender_state = self.versions[sender_head].full_state

        if not receiver_head:
            return {"full_state": sender_state, "mode": "full_transfer"}

        receiver_state = self.versions[receiver_head].full_state
        diff = compute_diff(receiver_state, sender_state)
        return {"diff": diff, "mode": "delta", "base_version": receiver_head}

def run_versioned_handoff(store: VersionedSnapshotStore, new_task: str) -> str:
    client = anthropic.Anthropic()

    # Agent A does research and commits snapshots
    state_v1 = {"facts": {"domain": "healthcare AI"}, "phase": "research", "turns": 5}
    store.commit("agent_researcher", state_v1)

    state_v2 = {**state_v1, "facts": {**state_v1["facts"], "key_finding": "FDA cleared 521 AI devices in 2023"}, "phase": "analysis", "turns": 12}
    store.commit("agent_researcher", state_v2)

    # Agent B gets a delta handoff
    handoff_package = store.get_diff_for_handoff("agent_researcher", "agent_writer")
    if handoff_package.get("mode") == "full_transfer":
        ctx = f"Full context: {json.dumps(handoff_package['full_state'])}"
    else:
        ctx = f"State delta: {json.dumps(handoff_package['diff'])}"

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": f"{ctx}\n\nTask: {new_task}"}],
    )
    return response.content[0].text

if __name__ == "__main__":
    store = VersionedSnapshotStore()
    result = run_versioned_handoff(store, "Write a blog post based on the research findings")
    print(result)

# Expected Token Savings: ~60-80% on delta handoffs between agents with shared history
# Environment: pip install anthropic
```

## Option 5: Content-Addressed Snapshot Deduplication

Hash each piece of the memory state independently. If two agents have accumulated the same tool results or facts (from accessing shared data sources), their snapshots share underlying content-addressed blocks. Only novel blocks transfer during handoff, dramatically reducing overhead when many agents work on related tasks.

```python
import anthropic
import json
import hashlib
import time
from dataclasses import dataclass, field
from typing import Any

@dataclass
class ContentBlock:
    block_id: str        # SHA-256 of content
    content: Any
    block_type: str      # "fact", "tool_result", "turn", "summary"
    created_at: float

class ContentAddressedStore:
    """Global store of content blocks, deduplicated by hash."""
    def __init__(self):
        self.blocks: dict[str, ContentBlock] = {}
        self.refs: int = 0  # total references stored

    def store(self, content: Any, block_type: str) -> str:
        block_id = hashlib.sha256(
            json.dumps(content, sort_keys=True).encode()
        ).hexdigest()[:16]
        if block_id not in self.blocks:
            self.blocks[block_id] = ContentBlock(
                block_id=block_id,
                content=content,
                block_type=block_type,
                created_at=time.time(),
            )
        self.refs += 1
        return block_id

    def retrieve(self, block_id: str) -> Any:
        return self.blocks[block_id].content

    def dedup_ratio(self) -> float:
        if self.refs == 0:
            return 0.0
        return 1.0 - len(self.blocks) / self.refs

@dataclass
class AgentMemory:
    agent_id: str
    fact_ids: list[str] = field(default_factory=list)
    tool_result_ids: list[str] = field(default_factory=list)
    turn_ids: list[str] = field(default_factory=list)
    task_state: dict = field(default_factory=dict)

class MultiAgentCoordinator:
    def __init__(self):
        self.store = ContentAddressedStore()
        self.agent_memories: dict[str, AgentMemory] = {}

    def get_or_create_memory(self, agent_id: str) -> AgentMemory:
        if agent_id not in self.agent_memories:
            self.agent_memories[agent_id] = AgentMemory(agent_id=agent_id)
        return self.agent_memories[agent_id]

    def add_fact(self, agent_id: str, fact: dict) -> None:
        mem = self.get_or_create_memory(agent_id)
        block_id = self.store.store(fact, "fact")
        if block_id not in mem.fact_ids:
            mem.fact_ids.append(block_id)

    def add_tool_result(self, agent_id: str, result: dict) -> None:
        mem = self.get_or_create_memory(agent_id)
        block_id = self.store.store(result, "tool_result")
        mem.tool_result_ids.append(block_id)

    def transfer_memory(self, from_agent: str, to_agent: str) -> int:
        """Transfer memory from one agent to another; returns blocks transferred."""
        src = self.get_or_create_memory(from_agent)
        dst = self.get_or_create_memory(to_agent)

        transferred = 0
        for fid in src.fact_ids:
            if fid not in dst.fact_ids:
                dst.fact_ids.append(fid)
                transferred += 1
        for rid in src.tool_result_ids:
            if rid not in dst.tool_result_ids:
                dst.tool_result_ids.append(rid)
                transferred += 1
        dst.task_state.update(src.task_state)
        return transferred

    def build_context(self, agent_id: str, max_facts: int = 10) -> str:
        mem = self.get_or_create_memory(agent_id)
        facts = [self.store.retrieve(fid) for fid in mem.fact_ids[-max_facts:]]
        tools = [self.store.retrieve(rid) for rid in mem.tool_result_ids[-5:]]
        return json.dumps({
            "facts": facts,
            "recent_tool_results": tools,
            "task_state": mem.task_state,
        }, indent=2)

def run_coordinated_agents(coordinator: MultiAgentCoordinator) -> str:
    client = anthropic.Anthropic()

    # Multiple agents accumulate overlapping facts
    shared_fact = {"dataset": "MIMIC-III", "size": "46k patients", "modality": "EHR"}
    for agent_id in ["agent_data", "agent_model", "agent_eval"]:
        coordinator.add_fact(agent_id, shared_fact)  # stored once, referenced 3×

    coordinator.add_fact("agent_data", {"preprocessing": "normalized vital signs, imputed missing labs"})
    coordinator.add_fact("agent_model", {"architecture": "Transformer with 6 layers", "accuracy": "0.89 AUROC"})
    coordinator.add_tool_result("agent_eval", {"tool": "stats_test", "result": "p=0.003, significant improvement"})

    # Transfer agent_eval memory to synthesizer
    transferred = coordinator.transfer_memory("agent_model", "agent_synth")
    transferred += coordinator.transfer_memory("agent_eval", "agent_synth")
    print(f"Transferred {transferred} unique blocks; dedup ratio: {coordinator.store.dedup_ratio():.1%}")

    ctx = coordinator.build_context("agent_synth")
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[{"role": "user", "content": f"Synthesize findings:\n{ctx}\n\nWrite a one-paragraph research summary."}],
    )
    return response.content[0].text

if __name__ == "__main__":
    coordinator = MultiAgentCoordinator()
    result = run_coordinated_agents(coordinator)
    print(result)

# Expected Token Savings: ~70% in multi-agent swarms with shared knowledge bases
# Environment: pip install anthropic
```

## Option 6: Streaming Snapshot with Priority Tiers

For very large agent memories, stream the snapshot to the receiving agent in priority tiers: critical state first (task goal, hard constraints, current phase), then important context (key facts, recent turns), then background detail (full history). The receiving agent starts working as soon as critical state arrives, progressively enriching its context without waiting for full transfer.

```python
import anthropic
import asyncio
import json
import time
from dataclasses import dataclass
from enum import IntEnum
from typing import AsyncIterator

class Priority(IntEnum):
    CRITICAL = 1    # task goal, hard constraints, current phase
    IMPORTANT = 2   # key facts, recent tool results, last 5 turns
    BACKGROUND = 3  # full history, all tool results, metadata

@dataclass
class MemoryChunk:
    priority: Priority
    chunk_type: str
    data: dict
    sequence: int

async def stream_snapshot(
    task_goal: str,
    constraints: list[str],
    phase: str,
    key_facts: dict,
    recent_turns: list[dict],
    full_history: list[dict],
) -> AsyncIterator[MemoryChunk]:
    """Yield memory chunks in priority order with simulated async retrieval."""
    # Priority 1: Critical (emit immediately)
    yield MemoryChunk(Priority.CRITICAL, "task_goal", {"goal": task_goal, "phase": phase}, 1)
    yield MemoryChunk(Priority.CRITICAL, "constraints", {"constraints": constraints}, 2)
    await asyncio.sleep(0)  # yield control

    # Priority 2: Important (emit after brief I/O)
    await asyncio.sleep(0.01)
    yield MemoryChunk(Priority.IMPORTANT, "key_facts", key_facts, 3)
    for i, turn in enumerate(recent_turns[-5:]):
        yield MemoryChunk(Priority.IMPORTANT, "recent_turn", turn, 4 + i)
    await asyncio.sleep(0)

    # Priority 3: Background (emit last, may be truncated)
    await asyncio.sleep(0.02)
    for i, turn in enumerate(full_history[:-5]):
        yield MemoryChunk(Priority.BACKGROUND, "history_turn", turn, 100 + i)

async def receive_and_execute_with_streaming_snapshot(
    snapshot_stream: AsyncIterator[MemoryChunk],
    new_instruction: str,
) -> str:
    client = anthropic.AsyncAnthropic()

    context_layers: dict[Priority, list] = {p: [] for p in Priority}
    first_response_sent = False
    final_response = ""

    async for chunk in snapshot_stream:
        context_layers[chunk.priority].append(chunk)

        # Once critical context arrives, start working immediately
        if chunk.priority == Priority.CRITICAL and not first_response_sent:
            critical_ctx = {
                c.chunk_type: c.data
                for c in context_layers[Priority.CRITICAL]
            }
            print(f"[StreamingHandoff] Critical context ready — starting preliminary work")

            response = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=256,
                messages=[{
                    "role": "user",
                    "content": f"CRITICAL CONTEXT: {json.dumps(critical_ctx)}\n\n{new_instruction}\n\n(More context loading — give preliminary response)",
                }],
            )
            print(f"[Preliminary] {response.content[0].text[:80]}...")
            first_response_sent = True

    # Final pass with full context
    all_chunks = [c for tier in context_layers.values() for c in tier]
    all_chunks.sort(key=lambda c: (c.priority, c.sequence))
    full_ctx = [{"type": c.chunk_type, "data": c.data} for c in all_chunks[:30]]

    response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": f"FULL CONTEXT ({len(all_chunks)} chunks):\n{json.dumps(full_ctx, indent=2)}\n\n{new_instruction}",
        }],
    )
    final_response = response.content[0].text
    return final_response

async def main():
    snapshot = stream_snapshot(
        task_goal="Design a fault-tolerant payment processing pipeline",
        constraints=["PCI-DSS compliant", "< 100ms p99 latency", "zero data loss"],
        phase="architecture_review",
        key_facts={
            "current_tps": 1200,
            "peak_tps": 8500,
            "failure_modes_identified": ["network partition", "db failover lag", "downstream timeout"],
        },
        recent_turns=[
            {"role": "user", "content": "What about idempotency?"},
            {"role": "assistant", "content": "Implemented via UUID dedup table with TTL..."},
            {"role": "user", "content": "Saga vs 2PC?"},
            {"role": "assistant", "content": "Saga with compensation is recommended for distributed..."},
        ],
        full_history=[{"role": "user", "content": f"Turn {i}"} for i in range(40)],
    )

    result = await receive_and_execute_with_streaming_snapshot(
        snapshot,
        "Produce the final architecture decision record (ADR)",
    )
    print("\n[Final ADR]\n", result)

if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: ~45% latency reduction via early start; handles arbitrarily large histories
# Environment: pip install anthropic
```

## Comparison

| Option | Transfer Size | Durability | Multi-Agent | Best For |
|--------|--------------|-----------|-------------|----------|
| 1. JSON Serialization | Full state | File | No | Simple 2-agent handoffs |
| 2. SQLite Store | Full state | DB | Yes | Production orchestration |
| 3. Async + Compression | Compressed | File/Memory | No | Large conversation histories |
| 4. Versioned + Diff | Delta only | Memory | Yes | Frequent collaborating agents |
| 5. Content-Addressed | Novel blocks | Memory | Yes | Swarms with shared knowledge |
| 6. Streaming Priority | Tiered | Memory | No | Real-time, low-latency handoffs |
