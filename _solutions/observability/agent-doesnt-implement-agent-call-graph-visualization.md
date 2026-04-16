---
title: "Agent Doesn't Implement Agent Call Graph Visualization"
slug: agent-doesnt-implement-agent-call-graph-visualization
category: observability
tags: [observability, call-graph, visualization, tracing, multi-agent, anthropic-sdk]
description: >
  In multi-agent systems the agent makes calls that fan out to sub-agents,
  tools, and APIs, but there is no record of which agent called which and in
  what order. Without a call graph it is impossible to debug non-deterministic
  failures, optimise the critical path, or explain agent behaviour to
  stakeholders.
symptoms:
  - A multi-agent workflow fails intermittently with no clear call chain in logs
  - No way to see which sub-agent took 80% of total latency
  - Stakeholders ask "why did the agent call X before Y?" and there is no answer
  - Cost attribution is impossible because calls are not linked to parent agents
related_solutions:
  - agent-doesnt-implement-distributed-trace-propagation
  - agent-doesnt-implement-slow-query-detection-for-tool-calls
  - agent-doesnt-implement-cost-per-conversation-tracking
---

## Problem

Multi-agent systems are directed acyclic graphs (or sometimes cyclic) of LLM
calls, tool invocations, and sub-agent delegations. When something goes wrong
or takes too long, engineers need to see the complete call graph — who called
whom, in what order, with what latency, and at what cost. Without explicit
call graph recording, debugging is guesswork.

---

## Solution 1 — In-Memory Call Graph with DOT Export

Record a call graph in memory as a list of directed edges. At the end of a
session, export it as a Graphviz DOT file for rendering.

```python
import anthropic
import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field


@dataclass
class CallNode:
    id:          str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    label:       str = ""
    agent:       str = ""
    type:        str = "llm"   # "llm" | "tool" | "agent"
    started_at:  float = field(default_factory=time.monotonic)
    ended_at:    float = 0.0
    cost_usd:    float = 0.0
    tokens_in:   int = 0
    tokens_out:  int = 0
    status:      str = "ok"

    def finish(self, **kwargs) -> None:
        self.ended_at = time.monotonic()
        for k, v in kwargs.items():
            setattr(self, k, v)

    @property
    def duration_ms(self) -> float:
        end = self.ended_at or time.monotonic()
        return (end - self.started_at) * 1000


@dataclass
class CallGraph:
    root_id:  str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    nodes:    dict[str, CallNode] = field(default_factory=dict)
    edges:    list[tuple[str, str]] = field(default_factory=list)  # (parent_id, child_id)

    def add_node(self, node: CallNode) -> CallNode:
        self.nodes[node.id] = node
        return node

    def add_edge(self, parent_id: str, child_id: str) -> None:
        self.edges.append((parent_id, child_id))

    def to_dot(self) -> str:
        lines = ["digraph CallGraph {", "  rankdir=LR;",
                 '  node [shape=box fontname="monospace"];']
        for node in self.nodes.values():
            color = {"llm": "lightblue", "tool": "lightyellow", "agent": "lightgreen"}.get(node.type, "white")
            label = (
                f"{node.label}\\n"
                f"{node.duration_ms:.0f}ms"
                + (f" ${node.cost_usd:.4f}" if node.cost_usd else "")
            )
            lines.append(f'  {node.id} [label="{label}" style=filled fillcolor={color}];')
        for p, c in self.edges:
            lines.append(f"  {p} -> {c};")
        lines.append("}")
        return "\n".join(lines)

    def summary(self) -> dict:
        total_ms   = sum(n.duration_ms for n in self.nodes.values())
        total_cost = sum(n.cost_usd for n in self.nodes.values())
        return {
            "nodes":      len(self.nodes),
            "edges":      len(self.edges),
            "total_ms":   round(total_ms, 1),
            "total_cost": round(total_cost, 6),
        }


PRICING = {"claude-haiku-4-5-20251001": (0.80, 4.00), "claude-sonnet-4-6": (3.00, 15.00)}


def _cost(model: str, inp: int, out: int) -> float:
    r = PRICING.get(model, (3.00, 15.00))
    return (inp * r[0] + out * r[1]) / 1_000_000


_graph = CallGraph()
_parent_stack: list[str] = []


async def tracked_llm_call(
    label: str,
    messages: list,
    model: str = "claude-sonnet-4-6",
    agent: str = "main",
) -> str:
    node = CallNode(label=label, agent=agent, type="llm")
    _graph.add_node(node)
    if _parent_stack:
        _graph.add_edge(_parent_stack[-1], node.id)
    _parent_stack.append(node.id)

    client = anthropic.AsyncAnthropic()
    try:
        resp = await client.messages.create(model=model, max_tokens=256, messages=messages)
        node.finish(
            tokens_in=resp.usage.input_tokens,
            tokens_out=resp.usage.output_tokens,
            cost_usd=_cost(model, resp.usage.input_tokens, resp.usage.output_tokens),
        )
        return resp.content[0].text
    except Exception as e:
        node.finish(status="error")
        raise
    finally:
        _parent_stack.pop()


async def tracked_tool_call(label: str, duration_s: float = 0.1) -> str:
    node = CallNode(label=label, type="tool")
    _graph.add_node(node)
    if _parent_stack:
        _graph.add_edge(_parent_stack[-1], node.id)
    await asyncio.sleep(duration_s)
    node.finish()
    return f"{label} result"


async def demo_call_graph():
    # Simulate a 2-level agent workflow
    root_id = uuid.uuid4().hex[:8]
    root = CallNode(id=root_id, label="orchestrator", type="agent")
    _graph.add_node(root)
    _parent_stack.append(root_id)

    # Orchestrator calls 2 LLMs and 1 tool
    classify = await tracked_llm_call("classify_intent", [{"role": "user", "content": "Route this query."}], model="claude-haiku-4-5-20251001")
    await tracked_tool_call("db_lookup", duration_s=0.08)
    answer = await tracked_llm_call("generate_answer", [{"role": "user", "content": "Answer based on DB result."}])

    _parent_stack.pop()
    root.finish()

    print("\nCall Graph DOT:")
    print(_graph.to_dot())
    print("\nSummary:", json.dumps(_graph.summary(), indent=2))


asyncio.run(demo_call_graph())
```

---

## Solution 2 — JSON-L Call Graph Stream for Real-Time Replay

Emit each call graph event as a JSONL line to stdout/file as it happens.
The stream can be consumed by a dashboard, stored for replay, or piped to
a graph renderer.

```python
import anthropic
import asyncio
import json
import sys
import time
import uuid
from contextlib import asynccontextmanager
from typing import IO


class CallGraphStream:
    def __init__(self, out: IO = sys.stdout):
        self._out = out
        self._session = uuid.uuid4().hex[:12]

    def _emit(self, event: dict) -> None:
        event["session"] = self._session
        event["wall_ts"] = time.time()
        print(json.dumps(event), file=self._out, flush=True)

    @asynccontextmanager
    async def span(
        self,
        name: str,
        span_type: str = "llm",
        parent_id: str | None = None,
        **attrs,
    ):
        span_id = uuid.uuid4().hex[:8]
        t0 = time.monotonic()
        self._emit({
            "event": "span_start",
            "span_id": span_id,
            "parent_id": parent_id,
            "name": name,
            "type": span_type,
            **attrs,
        })
        try:
            yield span_id
            self._emit({
                "event": "span_end",
                "span_id": span_id,
                "status": "ok",
                "duration_ms": round((time.monotonic() - t0) * 1000, 2),
            })
        except Exception as e:
            self._emit({
                "event": "span_end",
                "span_id": span_id,
                "status": "error",
                "error": str(e),
                "duration_ms": round((time.monotonic() - t0) * 1000, 2),
            })
            raise


_cg = CallGraphStream()


async def cg_llm(name: str, messages: list, parent_id: str | None = None,
                  model: str = "claude-sonnet-4-6") -> tuple[str, str]:
    async with _cg.span(name, span_type="llm", parent_id=parent_id, model=model) as sid:
        client = anthropic.AsyncAnthropic()
        resp = await client.messages.create(model=model, max_tokens=256, messages=messages)
        _cg._emit({
            "event": "span_attr",
            "span_id": sid,
            "input_tokens":  resp.usage.input_tokens,
            "output_tokens": resp.usage.output_tokens,
        })
        return resp.content[0].text, sid


async def cg_tool(name: str, parent_id: str | None = None, delay: float = 0.05) -> str:
    async with _cg.span(name, span_type="tool", parent_id=parent_id) as sid:
        await asyncio.sleep(delay)
        return f"{name} done"


async def demo_jsonl_graph():
    async with _cg.span("workflow", span_type="agent") as root_id:
        text1, s1 = await cg_llm("intent_classifier", [{"role": "user", "content": "Classify: book a flight"}], parent_id=root_id, model="claude-haiku-4-5-20251001")
        await cg_tool("flight_search", parent_id=root_id, delay=0.12)
        text2, s2 = await cg_llm("confirm_booking", [{"role": "user", "content": "Confirm flight details."}], parent_id=root_id)


asyncio.run(demo_jsonl_graph())
```

---

## Solution 3 — ASCII Call Tree Renderer for CLI Debugging

Build an in-memory call tree and render it as an indented ASCII tree for quick
CLI debugging without any external tooling.

```python
import anthropic
import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TreeNode:
    node_id:    str = field(default_factory=lambda: uuid.uuid4().hex[:6])
    name:       str = ""
    node_type:  str = "llm"
    duration_ms: float = 0.0
    tokens:     int = 0
    cost_usd:   float = 0.0
    status:     str = "ok"
    children:   list["TreeNode"] = field(default_factory=list)

    def render(self, prefix: str = "", is_last: bool = True) -> str:
        connector  = "└── " if is_last else "├── "
        child_pre  = "    " if is_last else "│   "
        icon = {"llm": "🤖", "tool": "🔧", "agent": "🎯"}.get(self.node_type, "•")
        cost_str = f" ${self.cost_usd:.4f}" if self.cost_usd else ""
        tok_str  = f" [{self.tokens}tok]" if self.tokens else ""
        status   = "" if self.status == "ok" else f" [{self.status}]"
        line = (f"{prefix}{connector}{icon} {self.name}"
                f"  {self.duration_ms:.0f}ms{tok_str}{cost_str}{status}\n")
        for i, child in enumerate(self.children):
            last = (i == len(self.children) - 1)
            line += child.render(prefix + child_pre, is_last=last)
        return line

    def total_cost(self) -> float:
        return self.cost_usd + sum(c.total_cost() for c in self.children)

    def critical_path_ms(self) -> float:
        if not self.children:
            return self.duration_ms
        return self.duration_ms + max(c.critical_path_ms() for c in self.children)


class CallTreeBuilder:
    def __init__(self):
        self._roots:  list[TreeNode] = []
        self._stack:  list[TreeNode] = []

    def push(self, name: str, node_type: str = "llm") -> TreeNode:
        node = TreeNode(name=name, node_type=node_type)
        if self._stack:
            self._stack[-1].children.append(node)
        else:
            self._roots.append(node)
        self._stack.append(node)
        return node

    def pop(self, node: TreeNode, duration_ms: float, **attrs) -> None:
        node.duration_ms = duration_ms
        for k, v in attrs.items():
            setattr(node, k, v)
        self._stack.pop()

    def render(self) -> str:
        lines = ["Call Tree\n" + "=" * 60]
        for root in self._roots:
            lines.append(root.render(prefix="", is_last=True).rstrip())
        total_cost = sum(r.total_cost() for r in self._roots)
        lines.append(f"\nTotal cost: ${total_cost:.6f}")
        return "\n".join(lines)


_tree = CallTreeBuilder()
PRICING = {"claude-haiku-4-5-20251001": (0.80, 4.00), "claude-sonnet-4-6": (3.00, 15.00)}


async def tree_llm(name: str, messages: list, model: str = "claude-sonnet-4-6") -> str:
    node = _tree.push(name, "llm")
    t0 = time.monotonic()
    client = anthropic.AsyncAnthropic()
    resp = await client.messages.create(model=model, max_tokens=128, messages=messages)
    r = PRICING.get(model, (3.0, 15.0))
    cost = (resp.usage.input_tokens * r[0] + resp.usage.output_tokens * r[1]) / 1_000_000
    _tree.pop(node, (time.monotonic() - t0) * 1000,
               tokens=resp.usage.output_tokens, cost_usd=cost)
    return resp.content[0].text


async def tree_tool(name: str, delay: float = 0.05) -> str:
    node = _tree.push(name, "tool")
    t0 = time.monotonic()
    await asyncio.sleep(delay)
    _tree.pop(node, (time.monotonic() - t0) * 1000)
    return f"{name} result"


async def demo_ascii_tree():
    root = _tree.push("research_agent", "agent")
    t0 = time.monotonic()

    await tree_llm("decompose_query", [{"role": "user", "content": "Decompose: explain distributed systems"}], model="claude-haiku-4-5-20251001")
    await tree_tool("web_search", delay=0.15)
    await tree_tool("db_lookup",  delay=0.08)
    await tree_llm("synthesize_answer", [{"role": "user", "content": "Synthesise findings into an answer."}])

    _tree.pop(root, (time.monotonic() - t0) * 1000)
    print(_tree.render())


asyncio.run(demo_ascii_tree())
```

---

## Solution 4 — Mermaid Diagram Generator for Documentation

Generate a Mermaid flowchart from the call graph that can be embedded directly
in Markdown files, GitHub READMEs, or Notion pages for stakeholder reporting.

```python
import anthropic
import asyncio
import time
import uuid
from dataclasses import dataclass, field


@dataclass
class MermaidNode:
    id:          str = field(default_factory=lambda: "N" + uuid.uuid4().hex[:6])
    label:       str = ""
    node_type:   str = "llm"
    duration_ms: float = 0.0
    tokens:      int = 0


@dataclass
class MermaidGraph:
    nodes: list[MermaidNode] = field(default_factory=list)
    edges: list[tuple[str, str, str]] = field(default_factory=list)  # (from, to, label)

    def add(self, node: MermaidNode) -> MermaidNode:
        self.nodes.append(node)
        return node

    def link(self, src: str, dst: str, label: str = "") -> None:
        self.edges.append((src, dst, label))

    def to_mermaid(self) -> str:
        lines = ["```mermaid", "flowchart LR"]
        for node in self.nodes:
            shape_open  = {"llm": "([", "tool": "[/", "agent": "[("}
            shape_close = {"llm": "])", "tool": "/]", "agent": ")]"}
            o = shape_open.get(node.node_type, "[")
            c = shape_close.get(node.node_type, "]")
            tok = f"\\n{node.tokens}tok" if node.tokens else ""
            ms  = f"\\n{node.duration_ms:.0f}ms" if node.duration_ms else ""
            lines.append(f'  {node.id}{o}"{node.label}{ms}{tok}"{c}')
        for src, dst, label in self.edges:
            arrow = f" --> |{label}|" if label else " --> "
            lines.append(f"  {src}{arrow}{dst}")
        lines.append("```")
        return "\n".join(lines)

    def to_markdown_section(self, title: str = "Agent Call Graph") -> str:
        return f"## {title}\n\n{self.to_mermaid()}\n"


_mg = MermaidGraph()
_parent_ids: list[str] = []
PRICING = {"claude-haiku-4-5-20251001": (0.80, 4.00), "claude-sonnet-4-6": (3.00, 15.00)}


async def mermaid_llm(label: str, messages: list, model: str = "claude-sonnet-4-6") -> str:
    node = MermaidNode(label=label, node_type="llm")
    _mg.add(node)
    if _parent_ids:
        _mg.link(_parent_ids[-1], node.id)
    _parent_ids.append(node.id)

    t0 = time.monotonic()
    client = anthropic.AsyncAnthropic()
    resp = await client.messages.create(model=model, max_tokens=128, messages=messages)
    node.duration_ms = (time.monotonic() - t0) * 1000
    node.tokens = resp.usage.output_tokens
    _parent_ids.pop()
    return resp.content[0].text


async def mermaid_tool(label: str, delay: float = 0.05) -> str:
    node = MermaidNode(label=label, node_type="tool")
    _mg.add(node)
    if _parent_ids:
        _mg.link(_parent_ids[-1], node.id)
    t0 = time.monotonic()
    await asyncio.sleep(delay)
    node.duration_ms = (time.monotonic() - t0) * 1000
    return f"{label} done"


async def demo_mermaid():
    root = MermaidNode(label="orchestrator", node_type="agent")
    _mg.add(root)
    _parent_ids.append(root.id)

    await mermaid_llm("classify", [{"role": "user", "content": "Classify query."}], model="claude-haiku-4-5-20251001")
    await mermaid_tool("search_db", delay=0.1)
    await mermaid_llm("generate", [{"role": "user", "content": "Generate response."}])

    _parent_ids.pop()

    diagram = _mg.to_markdown_section("Research Agent Call Graph")
    print(diagram)
    with open("/tmp/agent_call_graph.md", "w") as f:
        f.write(diagram)
    print("[mermaid] saved to /tmp/agent_call_graph.md")


asyncio.run(demo_mermaid())
```

---

## Solution 5 — Critical Path Analyser

From the call graph, identify the critical path — the sequence of dependent
calls that determines total latency. Highlight which calls to optimise for
the greatest latency reduction.

```python
import anthropic
import asyncio
import time
import uuid
from dataclasses import dataclass, field


@dataclass
class CPNode:
    id:          str = field(default_factory=lambda: uuid.uuid4().hex[:6])
    name:        str = ""
    duration_ms: float = 0.0
    is_parallel: bool = False   # True if this call runs in parallel with siblings
    children:    list["CPNode"] = field(default_factory=list)
    parent_id:   str | None = None

    def critical_path(self) -> list[tuple["CPNode", float]]:
        """Returns list of (node, cumulative_ms) along the critical path."""
        if not self.children:
            return [(self, self.duration_ms)]
        # Among children, find the one with the longest path
        child_paths = [c.critical_path() for c in self.children]
        longest = max(child_paths, key=lambda p: p[-1][1])
        return [(self, self.duration_ms)] + [(n, t + self.duration_ms) for n, t in longest]

    def total_sequential_ms(self) -> float:
        return self.duration_ms + sum(c.total_sequential_ms() for c in self.children)

    def parallelism_savings_ms(self) -> float:
        """How much time we saved by running parallel calls concurrently."""
        if len(self.children) <= 1:
            return 0.0
        sequential = sum(c.total_sequential_ms() for c in self.children)
        concurrent = max((c.total_sequential_ms() for c in self.children), default=0)
        return sequential - concurrent + sum(c.parallelism_savings_ms() for c in self.children)


class CPTracker:
    def __init__(self):
        self._root: CPNode | None = None
        self._stack: list[CPNode] = []

    def push(self, name: str) -> CPNode:
        node = CPNode(name=name)
        if self._stack:
            node.parent_id = self._stack[-1].id
            self._stack[-1].children.append(node)
        else:
            self._root = node
        self._stack.append(node)
        return node

    def pop(self, node: CPNode, duration_ms: float) -> None:
        node.duration_ms = duration_ms
        self._stack.pop()

    def report(self) -> str:
        if not self._root:
            return "No data"
        cp = self._root.critical_path()
        lines = ["Critical Path Analysis", "=" * 50]
        for node, cum_ms in cp:
            lines.append(f"  {node.name:<30} {node.duration_ms:8.1f}ms  (cumulative: {cum_ms:.1f}ms)")
        savings = self._root.parallelism_savings_ms()
        lines.append(f"\nParallelism savings: {savings:.1f}ms")
        lines.append(f"Recommendation: optimise '{cp[-1][0].name}' for largest latency reduction")
        return "\n".join(lines)


_cp = CPTracker()


async def cp_llm(name: str, messages: list, model: str = "claude-sonnet-4-6") -> str:
    node = _cp.push(name)
    t0 = time.monotonic()
    client = anthropic.AsyncAnthropic()
    resp = await client.messages.create(model=model, max_tokens=128, messages=messages)
    _cp.pop(node, (time.monotonic() - t0) * 1000)
    return resp.content[0].text


async def cp_tool(name: str, delay: float = 0.1) -> str:
    node = _cp.push(name)
    t0 = time.monotonic()
    await asyncio.sleep(delay)
    _cp.pop(node, (time.monotonic() - t0) * 1000)
    return "done"


async def demo_critical_path():
    root = _cp.push("workflow")
    t0 = time.monotonic()
    await cp_llm("classify", [{"role": "user", "content": "Route this query."}], model="claude-haiku-4-5-20251001")
    await cp_tool("slow_search", delay=0.5)   # this is the bottleneck
    await cp_llm("answer", [{"role": "user", "content": "Answer."}])
    _cp.pop(root, (time.monotonic() - t0) * 1000)
    print(_cp.report())


asyncio.run(demo_critical_path())
```

---

## Solution 6 — Live Call Graph SSE Feed for Real-Time Dashboard

Push call graph events over Server-Sent Events so a browser dashboard can
render the graph live as the agent executes, without polling.

```python
import anthropic
import asyncio
import json
import time
import uuid
from asyncio import Queue


class LiveCallGraphFeed:
    """Publishes call graph events to N subscriber queues."""

    def __init__(self):
        self._subscribers: list[Queue] = []
        self._session_id = uuid.uuid4().hex[:10]

    def subscribe(self) -> Queue:
        q: Queue = asyncio.Queue()
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: Queue) -> None:
        self._subscribers = [s for s in self._subscribers if s is not q]

    def _publish(self, event: dict) -> None:
        event["session"] = self._session_id
        event["ts"]      = time.time()
        for q in self._subscribers:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass

    def span_start(self, span_id: str, name: str,
                   span_type: str, parent_id: str | None = None) -> None:
        self._publish({
            "type": "span_start", "span_id": span_id,
            "name": name, "span_type": span_type, "parent_id": parent_id,
        })

    def span_end(self, span_id: str, duration_ms: float,
                 status: str = "ok", **attrs) -> None:
        self._publish({
            "type": "span_end", "span_id": span_id,
            "duration_ms": round(duration_ms, 2),
            "status": status, **attrs,
        })


_feed = LiveCallGraphFeed()


async def live_llm(name: str, messages: list, parent_id: str | None = None,
                   model: str = "claude-sonnet-4-6") -> tuple[str, str]:
    sid = uuid.uuid4().hex[:8]
    _feed.span_start(sid, name, "llm", parent_id)
    t0 = time.monotonic()
    client = anthropic.AsyncAnthropic()
    resp = await client.messages.create(model=model, max_tokens=128, messages=messages)
    _feed.span_end(sid, (time.monotonic() - t0) * 1000,
                   tokens_out=resp.usage.output_tokens)
    return resp.content[0].text, sid


async def live_tool(name: str, parent_id: str | None = None, delay: float = 0.1) -> str:
    sid = uuid.uuid4().hex[:8]
    _feed.span_start(sid, name, "tool", parent_id)
    t0 = time.monotonic()
    await asyncio.sleep(delay)
    _feed.span_end(sid, (time.monotonic() - t0) * 1000)
    return "done"


async def sse_consumer(q: Queue, label: str, n: int = 8) -> None:
    """Simulates a browser SSE client consuming events."""
    for _ in range(n):
        event = await asyncio.wait_for(q.get(), timeout=30.0)
        print(f"[SSE:{label}] {json.dumps(event)}")


async def demo_live_feed():
    q = _feed.subscribe()
    consumer = asyncio.create_task(sse_consumer(q, "browser", n=8))

    root_id = uuid.uuid4().hex[:8]
    _feed.span_start(root_id, "orchestrator", "agent")
    t0 = time.monotonic()

    _, s1 = await live_llm("classify", [{"role": "user", "content": "Classify."}],
                            parent_id=root_id, model="claude-haiku-4-5-20251001")
    await live_tool("db_lookup", parent_id=root_id, delay=0.08)
    _, s2 = await live_llm("generate", [{"role": "user", "content": "Generate."}], parent_id=root_id)

    _feed.span_end(root_id, (time.monotonic() - t0) * 1000)
    await consumer


asyncio.run(demo_live_feed())
```

---

## Comparison

| Approach | Output format | Real-time | Persistent | Stakeholder-friendly | Complexity |
|---|---|---|---|---|---|
| In-memory + DOT export | Graphviz DOT | No | File | Requires renderer | Low |
| JSONL stream | Machine-readable | Yes | File/log | No | Low |
| ASCII tree renderer | CLI text | No | No | Developers only | Low |
| Mermaid diagram | Markdown embed | No | File | Yes (GitHub/Notion) | Low |
| Critical path analyser | Text report | No | No | Engineering teams | Medium |
| SSE live feed | Browser events | Yes | No | Yes (dashboard) | Medium |

**Rule of thumb:**
- Dev debugging → ASCII tree (Solution 3) — instant, no dependencies
- Documentation / stakeholder reporting → Mermaid (Solution 4) — paste into any Markdown
- Optimisation work → critical path analyser (Solution 5) tells you exactly what to fix
- Operations dashboard → SSE live feed (Solution 6) with a simple JS graph renderer
- Audit logging → JSONL stream (Solution 2) — every event captured, filterable with jq
