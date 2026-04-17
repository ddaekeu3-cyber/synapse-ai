---
title: "Agent Doesn't Implement Lazy Tool Schema Loading"
description: "Agents that include the full schema of every registered tool in every prompt consume hundreds to thousands of tokens on tools that will never be called in the current conversation context. Implement lazy tool schema loading that includes only the schemas of tools likely to be needed, using intent classification or explicit tool groups, and injects the full schema of additional tools on demand when the agent requests them."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-lazy-tool-schema-loading
tags: [lazy-loading, tool-schema, token-efficiency, intent-classification, context-optimization, tool-selection]
symptoms:
  - "Tool definitions consume 2000+ tokens in every prompt regardless of the user's intent"
  - "Agent registered with 30 tools includes all 30 schemas even for simple greetings"
  - "Adding new tools increases token cost for every request, not just requests that use them"
  - "No grouping of tools by domain — all tools treated equally in every context"
  - "Tool schema tokens measured as fixed overhead — no analysis of which tools are actually invoked"
---

## Why This Happens

Tool schemas are verbose: each tool definition includes a name, description, parameter list with types and descriptions, and required/optional flags. A registry of 20 tools at 150 tokens each adds 3000 tokens to every prompt. Most conversations use a subset of 2–5 tools. Loading all schemas unconditionally pays the token cost for tools that are never invoked. Lazy loading requires classifying the incoming request to determine which tool groups are relevant, including only those schemas initially, and providing a mechanism to fetch additional schemas if the agent discovers it needs a tool not in the current context.

## Solution 1: Tool Group Definition

```python
from dataclasses import dataclass, field
from typing import List, Optional, Set


@dataclass
class ToolSchema:
    tool_name: str
    description: str
    parameters: dict         # JSON schema for parameters
    group_ids: List[str] = field(default_factory=list)
    token_estimate: int = 0  # estimated tokens for this schema

    def __post_init__(self) -> None:
        if not self.token_estimate:
            import json
            raw = json.dumps({"name": self.tool_name, "description": self.description, "parameters": self.parameters})
            self.token_estimate = max(1, len(raw) // 4)


@dataclass
class ToolGroup:
    group_id: str
    display_name: str
    intent_keywords: List[str]     # trigger words for this group
    tool_names: List[str]          # tools in this group
    always_include: bool = False   # if True, always loaded regardless of intent
    priority: int = 0              # lower = loaded first when budget is tight
```

## Solution 2: Intent-Based Tool Group Selector

```python
import re
from typing import List, Set


class IntentBasedToolGroupSelector:
    """
    Selects tool groups relevant to the current user query based on
    keyword matching against group intent_keywords.
    Returns a ranked list of group IDs to include.
    """

    def __init__(self, groups: List[ToolGroup]):
        self._groups = groups
        self._compiled = [
            (group, [re.compile(r"\b" + re.escape(kw) + r"\b", re.IGNORECASE) for kw in group.intent_keywords])
            for group in groups
        ]

    def select(self, query: str, max_groups: int = 5) -> List[str]:
        """Returns group_ids in priority order for this query."""
        selected: List[tuple] = []  # (priority, group_id)

        for group, patterns in self._compiled:
            if group.always_include:
                selected.append((group.priority, group.group_id))
                continue
            for pattern in patterns:
                if pattern.search(query):
                    selected.append((group.priority, group.group_id))
                    break

        # Sort by priority, deduplicate
        seen: Set[str] = set()
        result: List[str] = []
        for _, gid in sorted(selected):
            if gid not in seen:
                seen.add(gid)
                result.append(gid)
            if len(result) >= max_groups:
                break

        return result

    def always_included_groups(self) -> List[str]:
        return [g.group_id for g in self._groups if g.always_include]
```

## Solution 3: Lazy Tool Schema Registry

```python
from typing import Dict, List, Optional, Set


class LazyToolSchemaRegistry:
    """
    Stores all tool schemas but only returns them when explicitly requested
    by group or by name. Tracks which schemas have been loaded per request.
    """

    def __init__(self):
        self._schemas: Dict[str, ToolSchema] = {}       # tool_name -> schema
        self._groups: Dict[str, ToolGroup] = {}          # group_id -> group
        self._group_tools: Dict[str, List[str]] = {}     # group_id -> [tool_names]

    def register_tool(self, schema: ToolSchema) -> None:
        self._schemas[schema.tool_name] = schema

    def register_group(self, group: ToolGroup) -> None:
        self._groups[group.group_id] = group
        self._group_tools[group.group_id] = group.tool_names

    def schemas_for_groups(self, group_ids: List[str]) -> List[ToolSchema]:
        result: List[ToolSchema] = []
        seen: Set[str] = set()
        for gid in group_ids:
            for tool_name in self._group_tools.get(gid, []):
                if tool_name not in seen and tool_name in self._schemas:
                    seen.add(tool_name)
                    result.append(self._schemas[tool_name])
        return result

    def schema_for_tool(self, tool_name: str) -> Optional[ToolSchema]:
        return self._schemas.get(tool_name)

    def all_tool_names(self) -> List[str]:
        return list(self._schemas.keys())

    def token_cost(self, group_ids: List[str]) -> int:
        return sum(s.token_estimate for s in self.schemas_for_groups(group_ids))

    def total_token_cost(self) -> int:
        return sum(s.token_estimate for s in self._schemas.values())
```

## Solution 4: Token-Budget-Aware Schema Loader

```python
from typing import List, Tuple


class TokenBudgetAwareSchemaLoader:
    """
    Selects tool schemas to include within a token budget.
    Prioritizes groups in priority order and stops when budget is reached.
    Falls back to summary descriptions for tools that exceed the budget.
    """

    def __init__(
        self,
        registry: LazyToolSchemaRegistry,
        token_budget: int = 2000,
    ):
        self._registry = registry
        self._budget = token_budget

    def load(
        self,
        group_ids: List[str],
        extra_tool_names: List[str] = None,
    ) -> Tuple[List[ToolSchema], dict]:
        """
        Returns (schemas_to_include, stats).
        """
        extra_tool_names = extra_tool_names or []
        included: List[ToolSchema] = []
        used_tokens = 0
        skipped_groups = []

        for gid in group_ids:
            group_schemas = self._registry.schemas_for_groups([gid])
            group_tokens = sum(s.token_estimate for s in group_schemas)
            if used_tokens + group_tokens <= self._budget:
                for s in group_schemas:
                    if s.tool_name not in {i.tool_name for i in included}:
                        included.append(s)
                        used_tokens += s.token_estimate
            else:
                skipped_groups.append(gid)

        # Add specifically requested extra tools
        for tool_name in extra_tool_names:
            schema = self._registry.schema_for_tool(tool_name)
            if schema and schema.tool_name not in {i.tool_name for i in included}:
                if used_tokens + schema.token_estimate <= self._budget:
                    included.append(schema)
                    used_tokens += schema.token_estimate

        stats = {
            "included_tools": len(included),
            "used_tokens": used_tokens,
            "token_budget": self._budget,
            "skipped_groups": skipped_groups,
            "total_registry_tokens": self._registry.total_token_cost(),
            "token_savings": self._registry.total_token_cost() - used_tokens,
        }
        return included, stats
```

## Solution 5: On-Demand Schema Injector

```python
from typing import List, Optional


class OnDemandSchemaInjector:
    """
    Handles the case where the agent, during generation, requests a tool
    that was not included in the initial schema load. Injects the missing
    schema into the next prompt turn without restarting the conversation.
    """

    def __init__(
        self,
        registry: LazyToolSchemaRegistry,
        loader: TokenBudgetAwareSchemaLoader,
    ):
        self._registry = registry
        self._loader = loader
        self._injection_count = 0

    def inject_missing_tools(
        self,
        requested_tool_names: List[str],
        already_loaded: List[str],
    ) -> Optional[str]:
        """
        Returns a prompt snippet to prepend to the next turn,
        or None if all requested tools are already loaded.
        """
        missing = [t for t in requested_tool_names if t not in already_loaded]
        if not missing:
            return None

        schemas = []
        for name in missing:
            schema = self._registry.schema_for_tool(name)
            if schema:
                schemas.append(schema)

        if not schemas:
            return None

        self._injection_count += 1
        import json
        schema_text = "\n\n".join(
            f"Tool: {s.tool_name}\nDescription: {s.description}\nParameters: {json.dumps(s.parameters, indent=2)}"
            for s in schemas
        )
        return f"[Additional tool schemas loaded on demand:]\n{schema_text}"

    def stats(self) -> dict:
        return {"on_demand_injections": self._injection_count}
```

## Solution 6: Lazy Loading Savings Dashboard

```python
import time


class LazyToolLoadingSavingsDashboard:
    """
    Reports token savings from lazy loading versus always-include-all strategy.
    """

    def __init__(
        self,
        registry: LazyToolSchemaRegistry,
        loader: TokenBudgetAwareSchemaLoader,
        injector: OnDemandSchemaInjector,
    ):
        self._registry = registry
        self._loader = loader
        self._injector = injector
        self._load_stats_history = []

    def record_load(self, stats: dict) -> None:
        stats["ts"] = time.time()
        self._load_stats_history.append(stats)
        if len(self._load_stats_history) > 10000:
            self._load_stats_history = self._load_stats_history[-5000:]

    def render(self) -> dict:
        total_registry = self._registry.total_token_cost()
        recent = [s for s in self._load_stats_history if time.time() - s["ts"] < 3600]
        if recent:
            avg_used = sum(s["used_tokens"] for s in recent) / len(recent)
            avg_saved = total_registry - avg_used
        else:
            avg_used = avg_saved = 0

        return {
            "generated_at": time.time(),
            "registry": {
                "total_tools": len(self._registry._schemas),
                "total_token_cost": total_registry,
                "token_budget": self._loader._budget,
            },
            "last_hour": {
                "load_calls": len(recent),
                "avg_tokens_used": round(avg_used, 1),
                "avg_tokens_saved": round(avg_saved, 1),
                "avg_savings_pct": round(avg_saved / max(total_registry, 1) * 100, 1),
            },
            "on_demand_injections": self._injector.stats()["on_demand_injections"],
        }
```

## Comparison

| Approach | Intent Selection | Token Budget | On-Demand Injection | Group Prioritization | Savings Reporting |
|---|---|---|---|---|---|
| IntentBasedToolGroupSelector | Yes (keyword match) | No | No | Yes | No |
| LazyToolSchemaRegistry | No | No | No | Via groups | No |
| TokenBudgetAwareSchemaLoader | Via selector | Yes | No | Yes | Yes |
| OnDemandSchemaInjector | No | No | Yes | No | No |
| LazyToolLoadingSavingsDashboard | No | No | No | No | Yes |

**Best for production**: Measure actual tool invocation rates per conversation type before grouping — the data often shows that 80% of conversations use only 20% of tools. Set `always_include=True` only for 2–3 core tools (like a memory lookup or safety check) and load everything else lazily. Use `TokenBudgetAwareSchemaLoader` with a budget of 1500 tokens for tool schemas — leaving room for the system prompt, few-shot examples, and conversation history. Monitor `on_demand_injections` from `OnDemandSchemaInjector`: more than 5% of requests requiring on-demand injection means the intent classifier is misconfigured and needs retuning.
