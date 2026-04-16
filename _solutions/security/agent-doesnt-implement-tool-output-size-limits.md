---
title: "Agent Doesn't Implement Tool Output Size Limits"
description: "Agents that inject raw tool outputs directly into the LLM context without size bounds allow a single tool call to exhaust the entire context window, displace other results, or trigger runaway token consumption costs. Implement tool output size limits that truncate, summarize, or paginate oversized outputs before context injection, with per-tool policies and size violation audit logging."
date: 2026-04-16
difficulty: intermediate
category: security
slug: agent-doesnt-implement-tool-output-size-limits
tags: [output-size-limits, context-overflow, token-control, truncation-policy, output-sanitization, cost-control]
symptoms:
  - "A single database query result fills the entire context window"
  - "No maximum size enforced on tool outputs before they are injected into context"
  - "Tool outputs from different calls are displaced because one result is unexpectedly large"
  - "LLM token costs spike when a tool returns a 100KB JSON blob"
  - "Agents process files or web pages in full even when only a summary is needed"
---

## Why This Happens

Tool outputs are passed to the LLM as-is in most agent implementations. A web scrape tool may return 50KB of HTML, a database tool may return thousands of rows, and a file read tool may return a multi-megabyte document — all injected verbatim into the context. Without a size enforcement layer between tool execution and context assembly, any tool can dominate the context. The fix requires per-tool size policies, a truncation or summarization strategy applied at the enforcement layer, and audit logging that records violations for cost and security analysis.

## Solution 1: Output Size Policy

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class OversizeStrategy(str, Enum):
    TRUNCATE = "truncate"           # cut to max_chars, append truncation notice
    SUMMARIZE = "summarize"         # replace with a summary (requires summarize_fn)
    PAGINATE = "paginate"           # return first page only with page metadata
    REJECT = "reject"               # raise an error; do not inject
    DROP = "drop"                   # silently omit the result


@dataclass
class ToolOutputSizePolicy:
    tool_name: str
    max_chars: int = 8000
    max_items: Optional[int] = None     # for list/array outputs
    strategy: OversizeStrategy = OversizeStrategy.TRUNCATE
    truncation_notice: str = "\n[OUTPUT TRUNCATED — {remaining} chars omitted]"
    page_size_chars: int = 4000
```

## Solution 2: Output Size Enforcer

```python
import json
from typing import Any, Callable, List, Optional


class ToolOutputSizeEnforcer:
    """
    Applies a ToolOutputSizePolicy to a raw tool output.
    Converts non-string outputs to JSON for size measurement.
    """

    def __init__(
        self,
        summarize_fn: Optional[Callable[[str, int], str]] = None,
    ):
        self._summarize_fn = summarize_fn

    def _to_str(self, output: Any) -> str:
        if isinstance(output, str):
            return output
        try:
            return json.dumps(output, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(output)

    def enforce(
        self,
        output: Any,
        policy: ToolOutputSizePolicy,
    ) -> dict:
        text = self._to_str(output)
        original_chars = len(text)

        if original_chars <= policy.max_chars:
            if policy.max_items is not None and isinstance(output, list):
                if len(output) > policy.max_items:
                    trimmed = output[:policy.max_items]
                    return {
                        "content": self._to_str(trimmed),
                        "truncated": True,
                        "original_chars": original_chars,
                        "final_chars": len(self._to_str(trimmed)),
                        "strategy": "item_limit",
                    }
            return {
                "content": text,
                "truncated": False,
                "original_chars": original_chars,
                "final_chars": original_chars,
                "strategy": "none",
            }

        strategy = policy.strategy

        if strategy == OversizeStrategy.REJECT:
            raise ValueError(
                f"Tool '{policy.tool_name}' output exceeds {policy.max_chars} chars "
                f"(got {original_chars})"
            )

        if strategy == OversizeStrategy.DROP:
            return {
                "content": "",
                "truncated": True,
                "dropped": True,
                "original_chars": original_chars,
                "final_chars": 0,
                "strategy": "drop",
            }

        if strategy == OversizeStrategy.SUMMARIZE and self._summarize_fn:
            summary = self._summarize_fn(text, policy.max_chars)
            return {
                "content": summary,
                "truncated": True,
                "original_chars": original_chars,
                "final_chars": len(summary),
                "strategy": "summarize",
            }

        if strategy == OversizeStrategy.PAGINATE:
            page = text[:policy.page_size_chars]
            remaining_pages = (original_chars - policy.page_size_chars) // policy.page_size_chars + 1
            page += f"\n[PAGE 1 OF ~{remaining_pages + 1} — use pagination to retrieve more]"
            return {
                "content": page,
                "truncated": True,
                "original_chars": original_chars,
                "final_chars": len(page),
                "strategy": "paginate",
                "total_pages_estimate": remaining_pages + 1,
            }

        # Default: TRUNCATE
        truncated = text[:policy.max_chars]
        remaining = original_chars - policy.max_chars
        notice = policy.truncation_notice.format(remaining=remaining)
        content = truncated + notice
        return {
            "content": content,
            "truncated": True,
            "original_chars": original_chars,
            "final_chars": len(content),
            "strategy": "truncate",
        }
```

## Solution 3: Output Size Policy Registry

```python
from typing import Dict, Optional


class ToolOutputSizePolicyRegistry:
    """
    Stores per-tool size policies. Returns a conservative default
    for tools without an explicit policy.
    """

    DEFAULT_MAX_CHARS = 8000

    def __init__(self, global_max_chars: int = DEFAULT_MAX_CHARS):
        self._policies: Dict[str, ToolOutputSizePolicy] = {}
        self._global_max = global_max_chars

    def register(self, policy: ToolOutputSizePolicy) -> None:
        self._policies[policy.tool_name] = policy

    def get(self, tool_name: str) -> ToolOutputSizePolicy:
        if tool_name in self._policies:
            return self._policies[tool_name]
        return ToolOutputSizePolicy(
            tool_name=tool_name,
            max_chars=self._global_max,
            strategy=OversizeStrategy.TRUNCATE,
        )

    def all_policies(self) -> Dict[str, dict]:
        return {
            name: {
                "max_chars": p.max_chars,
                "strategy": p.strategy.value,
            }
            for name, p in self._policies.items()
        }
```

## Solution 4: Size-Limited Tool Output Gateway

```python
import time
from typing import Any, Callable, Dict, List, Optional


class SizeLimitedToolOutputGateway:
    """
    Intercepts tool execution results and enforces size policies
    before passing output to the context assembler.
    """

    def __init__(
        self,
        registry: ToolOutputSizePolicyRegistry,
        enforcer: ToolOutputSizeEnforcer,
    ):
        self._registry = registry
        self._enforcer = enforcer
        self._violation_log: List[dict] = []

    async def process(
        self,
        tool_name: str,
        raw_output: Any,
        dispatch_metadata: Optional[dict] = None,
    ) -> dict:
        policy = self._registry.get(tool_name)
        enforcement = self._enforcer.enforce(raw_output, policy)

        if enforcement["truncated"]:
            self._violation_log.append({
                "ts": time.time(),
                "tool_name": tool_name,
                "original_chars": enforcement["original_chars"],
                "final_chars": enforcement["final_chars"],
                "strategy": enforcement["strategy"],
            })

        return {
            "tool_name": tool_name,
            "content": enforcement["content"],
            "size_enforced": enforcement["truncated"],
            "original_chars": enforcement["original_chars"],
            "final_chars": enforcement["final_chars"],
            "strategy": enforcement["strategy"],
        }

    def recent_violations(self, limit: int = 50) -> List[dict]:
        return self._violation_log[-limit:]

    def violation_summary(self) -> dict:
        total = len(self._violation_log)
        by_tool: Dict[str, int] = {}
        for v in self._violation_log:
            by_tool[v["tool_name"]] = by_tool.get(v["tool_name"], 0) + 1
        return {
            "total_violations": total,
            "by_tool": by_tool,
        }
```

## Solution 5: Output Size Budget Allocator

```python
from typing import Any, Dict, List, Optional, Tuple


class OutputSizeBudgetAllocator:
    """
    Given a total context budget (in chars) and a list of tool outputs,
    allocates per-tool size limits proportionally or by priority so that
    combined output never exceeds the budget.
    """

    def __init__(self, total_budget_chars: int = 32000):
        self._budget = total_budget_chars

    def allocate(
        self,
        tool_names: List[str],
        priorities: Optional[Dict[str, int]] = None,
    ) -> Dict[str, int]:
        """
        Returns a dict of tool_name -> allocated_chars.
        Higher priority tools get larger allocations.
        """
        if not tool_names:
            return {}
        prio = priorities or {}
        weights = {name: prio.get(name, 1) for name in tool_names}
        total_weight = sum(weights.values())
        return {
            name: max(500, int(self._budget * w / total_weight))
            for name, w in weights.items()
        }

    def rebalance(
        self,
        tool_outputs: List[Tuple[str, int]],  # (tool_name, actual_chars)
    ) -> int:
        """
        Returns remaining budget after accounting for actual usage.
        """
        used = sum(chars for _, chars in tool_outputs)
        return max(0, self._budget - used)
```

## Solution 6: Output Size Violation Auditor

```python
import time
from typing import List


class OutputSizeViolationAuditor:
    """
    Aggregates size violation events over time and surfaces which
    tools most frequently exceed their limits and by how much.
    """

    def __init__(self):
        self._events: List[dict] = []

    def record(self, gateway_violation: dict) -> None:
        self._events.append({**gateway_violation, "audited_at": time.time()})

    def top_violating_tools(self, n: int = 10, window_seconds: float = 86400.0) -> List[dict]:
        cutoff = time.time() - window_seconds
        recent = [e for e in self._events if e.get("audited_at", 0) >= cutoff]
        by_tool: dict = {}
        for e in recent:
            name = e["tool_name"]
            if name not in by_tool:
                by_tool[name] = {"count": 0, "total_excess_chars": 0}
            by_tool[name]["count"] += 1
            by_tool[name]["total_excess_chars"] += (
                e.get("original_chars", 0) - e.get("final_chars", 0)
            )
        ranked = sorted(by_tool.items(), key=lambda x: x[1]["count"], reverse=True)
        return [{"tool_name": k, **v} for k, v in ranked[:n]]
```

## Comparison

| Approach | Per-Tool Policy | Multi-Strategy | Budget Allocation | Violation Log | Audit |
|---|---|---|---|---|---|
| ToolOutputSizeEnforcer | Via policy | Yes (5 strategies) | No | No | No |
| ToolOutputSizePolicyRegistry | Yes | No | No | No | No |
| SizeLimitedToolOutputGateway | Via registry | Via enforcer | No | Yes | No |
| OutputSizeBudgetAllocator | No | No | Yes (priority) | No | No |
| OutputSizeViolationAuditor | No | No | No | No | Yes |

**Best for production**: Set a global default of `max_chars=8000` — this covers ~2000 tokens, leaving headroom for multi-tool responses. Override with larger limits only for tools that genuinely need it (e.g., `max_chars=20000` for a code search tool) and use `PAGINATE` strategy so the agent can request subsequent pages when the first page is insufficient. Use `OutputSizeBudgetAllocator` when more than three tools are called in parallel to prevent any single large result from displacing others. Log every violation with `SizeLimitedToolOutputGateway` and run `OutputSizeViolationAuditor.top_violating_tools()` weekly — consistently oversized outputs from the same tool signal that the tool itself needs a server-side limit, not just client-side truncation.
