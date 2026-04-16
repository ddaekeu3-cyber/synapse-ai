---
title: "Agent Doesn't Implement LLM Call Dependency Graph Tracking"
description: "Agents that spawn sub-agents, call tools that make their own LLM calls, or use multi-step reasoning chains produce a tree of LLM invocations — but log each call in isolation. Without a call dependency graph, it is impossible to determine which parent LLM call triggered a child call, how much of the total cost was incurred by a particular branch, or why a specific sub-call was made. Implement LLM call dependency graph tracking that records parent-child relationships, per-node cost, and propagates trace context through all call boundaries."
date: 2026-04-16
difficulty: advanced
category: observability
slug: agent-doesnt-implement-llm-call-dependency-graph-tracking
tags: [llm-call-graph, dependency-tracking, cost-attribution, call-tree, sub-agent-tracing, multi-step-observability]
symptoms:
  - "Cannot determine which root user request caused a specific LLM sub-call"
  - "Total cost per user request is unknown because sub-agent calls are not linked to the parent"
  - "Call trees have no depth or fan-out measurements — impossible to detect runaway recursion"
  - "Logs show dozens of isolated LLM calls with no indication of which triggered which"
  - "No way to replay or reconstruct the full call tree for a failed multi-step workflow"
---

## Why This Happens

When an agent calls an LLM to decide which tool to use, and that tool internally calls another LLM for summarization, and a sub-agent spawned by the first LLM calls yet another LLM — three LLM invocations have occurred with no link between them in the logs. Each call gets its own request ID, but there is no parent-call reference. Reconstructing the dependency graph from timestamps alone is unreliable. The fix is to propagate a `call_id` and `parent_call_id` through all LLM call boundaries — similar to distributed tracing span propagation — so that every LLM invocation records who asked for it.

## Solution 1: LLM Call Node

```python
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional


class LLMCallStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class LLMCallCost:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: float = 0.0

    def add(self, other: "LLMCallCost") -> "LLMCallCost":
        return LLMCallCost(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cost_usd=round(self.cost_usd + other.cost_usd, 6),
        )


@dataclass
class LLMCallNode:
    call_id: str
    model: str
    purpose: str                       # human label: "tool_selection", "summarize", etc.
    parent_call_id: Optional[str] = None
    root_call_id: Optional[str] = None
    status: LLMCallStatus = LLMCallStatus.PENDING
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    cost: LLMCallCost = field(default_factory=LLMCallCost)
    error: Optional[str] = None
    metadata: Dict[str, str] = field(default_factory=dict)
    depth: int = 0                     # 0 = root call

    def duration_ms(self) -> Optional[float]:
        if self.started_at and self.completed_at:
            return round((self.completed_at - self.started_at) * 1000, 2)
        return None
```

## Solution 2: Call Graph Store

```python
import time
from threading import Lock
from typing import Dict, List, Optional


class LLMCallGraphStore:
    """
    In-memory store for LLM call nodes, with parent-child indexing.
    Supports subtree queries for cost rollup and fan-out analysis.
    """

    def __init__(self, max_nodes: int = 50000, ttl_seconds: float = 7200.0):
        self._nodes: Dict[str, LLMCallNode] = {}
        self._children: Dict[str, List[str]] = {}
        self._roots: List[str] = []
        self._created_at: Dict[str, float] = {}
        self._max = max_nodes
        self._ttl = ttl_seconds
        self._lock = Lock()

    def add(self, node: LLMCallNode) -> None:
        with self._lock:
            self._evict_stale()
            self._nodes[node.call_id] = node
            self._created_at[node.call_id] = time.time()
            if node.parent_call_id:
                self._children.setdefault(node.parent_call_id, []).append(node.call_id)
            else:
                self._roots.append(node.call_id)

    def update(self, node: LLMCallNode) -> None:
        with self._lock:
            self._nodes[node.call_id] = node

    def get(self, call_id: str) -> Optional[LLMCallNode]:
        with self._lock:
            return self._nodes.get(call_id)

    def children(self, call_id: str) -> List[LLMCallNode]:
        with self._lock:
            child_ids = self._children.get(call_id, [])
            return [self._nodes[cid] for cid in child_ids if cid in self._nodes]

    def subtree(self, call_id: str) -> List[LLMCallNode]:
        """Returns all nodes in the subtree rooted at call_id (BFS)."""
        with self._lock:
            result = []
            queue = [call_id]
            while queue:
                current = queue.pop(0)
                node = self._nodes.get(current)
                if node:
                    result.append(node)
                    queue.extend(self._children.get(current, []))
            return result

    def _evict_stale(self) -> None:
        cutoff = time.time() - self._ttl
        stale = [cid for cid, ts in self._created_at.items() if ts < cutoff]
        for cid in stale[:100]:  # evict in batches
            self._nodes.pop(cid, None)
            self._created_at.pop(cid, None)
            self._children.pop(cid, None)
```

## Solution 3: Call Graph Context

```python
import contextvars
import uuid
from typing import Optional


_CALL_GRAPH_CTX: contextvars.ContextVar[Optional["CallGraphContext"]] = contextvars.ContextVar(
    "llm_call_graph_ctx", default=None
)


class CallGraphContext:
    """
    Tracks the current call position in the LLM call graph.
    Passed through asyncio context boundaries automatically.
    """

    def __init__(
        self,
        call_id: str,
        parent_call_id: Optional[str],
        root_call_id: str,
        depth: int,
    ):
        self.call_id = call_id
        self.parent_call_id = parent_call_id
        self.root_call_id = root_call_id
        self.depth = depth

    @classmethod
    def new_root(cls) -> "CallGraphContext":
        root_id = uuid.uuid4().hex[:16]
        return cls(call_id=root_id, parent_call_id=None, root_call_id=root_id, depth=0)

    def child(self) -> "CallGraphContext":
        return CallGraphContext(
            call_id=uuid.uuid4().hex[:16],
            parent_call_id=self.call_id,
            root_call_id=self.root_call_id,
            depth=self.depth + 1,
        )


def current_call_ctx() -> Optional[CallGraphContext]:
    return _CALL_GRAPH_CTX.get()


def set_call_ctx(ctx: CallGraphContext) -> contextvars.Token:
    return _CALL_GRAPH_CTX.set(ctx)
```

## Solution 4: Traced LLM Client

```python
import asyncio
import time
from typing import Any, Callable, Optional


class TracedLLMClient:
    """
    Wraps any LLM call function with call graph recording.
    Automatically inherits parent context from the current async context.
    """

    def __init__(
        self,
        store: LLMCallGraphStore,
        model: str,
        cost_per_input_token: float = 0.000003,
        cost_per_output_token: float = 0.000015,
    ):
        self._store = store
        self._model = model
        self._cost_in = cost_per_input_token
        self._cost_out = cost_per_output_token

    async def call(
        self,
        llm_fn: Callable,
        purpose: str,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        parent_ctx = current_call_ctx()
        if parent_ctx is None:
            ctx = CallGraphContext.new_root()
        else:
            ctx = parent_ctx.child()

        node = LLMCallNode(
            call_id=ctx.call_id,
            model=self._model,
            purpose=purpose,
            parent_call_id=ctx.parent_call_id,
            root_call_id=ctx.root_call_id,
            depth=ctx.depth,
            status=LLMCallStatus.RUNNING,
            started_at=time.time(),
        )
        self._store.add(node)
        token = set_call_ctx(ctx)

        try:
            result = await llm_fn(*args, **kwargs)
            usage = getattr(result, "usage", None)
            if usage:
                node.cost = LLMCallCost(
                    input_tokens=getattr(usage, "input_tokens", 0),
                    output_tokens=getattr(usage, "output_tokens", 0),
                    cost_usd=round(
                        getattr(usage, "input_tokens", 0) * self._cost_in
                        + getattr(usage, "output_tokens", 0) * self._cost_out,
                        6,
                    ),
                )
            node.status = LLMCallStatus.COMPLETED
            node.completed_at = time.time()
            self._store.update(node)
            return result
        except Exception as exc:
            node.status = LLMCallStatus.FAILED
            node.error = str(exc)
            node.completed_at = time.time()
            self._store.update(node)
            raise
        finally:
            _CALL_GRAPH_CTX.reset(token)
```

## Solution 5: Call Graph Analyzer

```python
from typing import Dict, List, Optional


class CallGraphAnalyzer:
    """
    Computes aggregate statistics over an LLM call subtree:
    total cost, max depth, fan-out, and failure rate.
    """

    def __init__(self, store: LLMCallGraphStore):
        self._store = store

    def subtree_cost(self, root_call_id: str) -> LLMCallCost:
        nodes = self._store.subtree(root_call_id)
        total = LLMCallCost()
        for node in nodes:
            total = total.add(node.cost)
        return total

    def max_depth(self, root_call_id: str) -> int:
        nodes = self._store.subtree(root_call_id)
        return max((n.depth for n in nodes), default=0)

    def fan_out(self, call_id: str) -> int:
        return len(self._store.children(call_id))

    def failure_rate(self, root_call_id: str) -> float:
        nodes = self._store.subtree(root_call_id)
        if not nodes:
            return 0.0
        failed = sum(1 for n in nodes if n.status == LLMCallStatus.FAILED)
        return round(failed / len(nodes), 4)

    def summarize(self, root_call_id: str) -> dict:
        nodes = self._store.subtree(root_call_id)
        cost = self.subtree_cost(root_call_id)
        return {
            "root_call_id": root_call_id,
            "total_nodes": len(nodes),
            "max_depth": self.max_depth(root_call_id),
            "total_cost_usd": cost.cost_usd,
            "total_input_tokens": cost.input_tokens,
            "total_output_tokens": cost.output_tokens,
            "failure_rate": self.failure_rate(root_call_id),
        }
```

## Solution 6: Call Graph Dashboard

```python
import time


class LLMCallGraphDashboard:
    """
    Renders a live snapshot of recent call trees with cost and depth summaries.
    """

    def __init__(self, store: LLMCallGraphStore, analyzer: CallGraphAnalyzer):
        self._store = store
        self._analyzer = analyzer

    def render(self, root_call_ids: List[str]) -> dict:
        summaries = []
        for root_id in root_call_ids:
            node = self._store.get(root_id)
            if node:
                summaries.append({
                    "purpose": node.purpose,
                    **self._analyzer.summarize(root_id),
                })
        return {
            "generated_at": time.time(),
            "call_trees": summaries,
        }
```

## Comparison

| Approach | Parent-Child Links | Cost Rollup | Depth Tracking | Context Propagation | Dashboard |
|---|---|---|---|---|---|
| LLMCallGraphStore | Yes (index) | No | No | No | No |
| CallGraphContext | No | No | Yes | Yes (ContextVar) | No |
| TracedLLMClient | Via context | Per-node | Via context | Yes | No |
| CallGraphAnalyzer | Via store | Yes (subtree) | Yes | No | No |
| LLMCallGraphDashboard | No | No | No | No | Yes |

**Best for production**: Wrap every LLM call site with `TracedLLMClient.call()` from day one — retrofitting after the fact requires touching every call site anyway, and the overhead is negligible. Set a `max_depth` alert threshold of 10 — call trees deeper than 10 almost always indicate unintended recursion or a prompt that causes the agent to spawn chains of sub-agents without a base case. Use `subtree_cost()` to charge LLM costs back to the originating user request — root-level cost attribution without subtree rollup consistently undercounts by 2-5× in multi-step agents. Store the call graph in Redis with a 2-hour TTL so it survives agent restarts and can be queried during post-incident review.
