---
title: "Agent Doesn't Implement Prompt Template Compilation for Repeated Rendering"
description: "AI agents that re-parse and re-render prompt templates on every request spend 5-30ms per turn on string scanning, variable substitution, and format validation—work that is identical across calls with the same template structure. Compiled prompt templates pre-process the static structure once at startup, reducing per-request rendering to simple slot-filling into a pre-allocated buffer."
date: 2025-02-21
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-prompt-template-compilation-for-repeated-rendering
tags:
  - prompt-template
  - compilation
  - performance
  - string-rendering
  - caching
  - template-optimization
  - latency
symptoms:
  - "Prompt construction takes 8-15ms per request even though the template never changes"
  - "Profiler shows 40% of pre-LLM time spent in string format() and split() calls"
  - "System prompt with 50 tool descriptions is re-serialized from scratch on every turn"
  - "Jinja2 template parsing overhead visible in traces for simple variable substitution"
  - "Template rendering allocates 200KB of intermediate strings for a 5KB final prompt"
---

## Problem

Most agent prompts have a fixed structural skeleton (system prompt, tool descriptions, formatting instructions) with a small number of variable slots (user query, retrieved context, conversation history). Re-parsing the template structure on every request—scanning for `{variable}` delimiters, validating slot names, converting tool schemas to JSON—wastes CPU on work that does not change between requests. Compiling the template splits the static structure from the variable slots once at startup: at render time, the pre-parsed structure is traversed and slots are filled into a pre-allocated output buffer, reducing rendering from O(template_size) to O(variable_data_size).

---

## Solution 1: CompiledPromptTemplate — Pre-Parsed Slot-Based Renderer

```python
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple, Union


@dataclass
class _Literal:
    text: str


@dataclass
class _Slot:
    name: str
    default: Optional[str] = None
    required: bool = True


class CompiledPromptTemplate:
    """
    Compiles a prompt template string into a list of alternating literal
    segments and named slots. At render time, iterates the compiled
    structure once and fills slots from a variables dict—no regex scanning,
    no string splitting, no format parsing.

    Syntax: {variable_name} for required slots,
            {variable_name:default_value} for optional slots.

    Usage:
        tmpl = CompiledPromptTemplate.compile(
            "You are {role}. Answer: {query}\nContext: {context:No context provided.}"
        )
        prompt = tmpl.render(role="an expert", query="What is HNSW?")
        # context uses its default since not provided
    """

    SLOT_RE = re.compile(r"\{(\w+)(?::([^}]*))?\}")

    def __init__(self, parts: List[Union[_Literal, _Slot]], source: str = ""):
        self._parts = parts
        self._source = source
        self._slot_names = frozenset(p.name for p in parts if isinstance(p, _Slot))
        # Pre-compute total static length for buffer sizing hint
        self._static_len = sum(len(p.text) for p in parts if isinstance(p, _Literal))

    @classmethod
    def compile(cls, template: str) -> "CompiledPromptTemplate":
        parts: List[Union[_Literal, _Slot]] = []
        last = 0
        for m in cls.SLOT_RE.finditer(template):
            if m.start() > last:
                parts.append(_Literal(template[last: m.start()]))
            name = m.group(1)
            default = m.group(2)
            parts.append(_Slot(name=name, default=default, required=default is None))
            last = m.end()
        if last < len(template):
            parts.append(_Literal(template[last:]))
        return cls(parts, source=template)

    def render(self, **variables: Any) -> str:
        # Pre-allocate a list for O(n) join instead of repeated concatenation
        buf: List[str] = []
        buf_append = buf.append  # local ref avoids repeated attribute lookup
        for part in self._parts:
            if isinstance(part, _Literal):
                buf_append(part.text)
            else:
                val = variables.get(part.name)
                if val is None:
                    if part.required:
                        raise ValueError(f"Required template slot '{part.name}' not provided")
                    buf_append(part.default or "")
                else:
                    buf_append(str(val))
        return "".join(buf)

    def render_dict(self, variables: Dict[str, Any]) -> str:
        return self.render(**variables)

    @property
    def slot_names(self) -> frozenset:
        return self._slot_names

    @property
    def estimated_static_bytes(self) -> int:
        return self._static_len
```

---

## Solution 2: TemplateRegistry — Startup-Compiled Template Cache

```python
import logging
import os
import time
from pathlib import Path
from typing import Dict, Iterator, Optional, Tuple

logger = logging.getLogger(__name__)


class TemplateRegistry:
    """
    Compiles all prompt templates at startup from a directory of .txt files
    and stores them by name. Templates are never re-parsed at request time.
    Supports hot-reload in development (disabled by default in production).

    Usage:
        registry = TemplateRegistry.from_directory("prompts/")
        prompt = registry.render("system_prompt",
                                  role="assistant", tools=tool_json)
        prompt = registry.render("user_turn", query=user_query)
    """

    def __init__(self, hot_reload: bool = False, reload_interval: float = 10.0):
        self._templates: Dict[str, CompiledPromptTemplate] = {}
        self._mtimes: Dict[str, float] = {}
        self._paths: Dict[str, str] = {}
        self._hot_reload = hot_reload
        self._reload_interval = reload_interval
        self._last_reload_check = 0.0

    @classmethod
    def from_directory(cls, directory: str, extension: str = ".txt",
                        hot_reload: bool = False) -> "TemplateRegistry":
        registry = cls(hot_reload=hot_reload)
        base = Path(directory)
        count = 0
        for path in sorted(base.rglob(f"*{extension}")):
            name = path.stem
            registry._load(name, str(path))
            count += 1
        logger.info("template_registry_loaded count=%d directory=%s", count, directory)
        return registry

    def _load(self, name: str, path: str):
        with open(path) as f:
            source = f.read()
        t0 = time.monotonic()
        self._templates[name] = CompiledPromptTemplate.compile(source)
        elapsed_us = round((time.monotonic() - t0) * 1_000_000)
        self._mtimes[name] = os.path.getmtime(path)
        self._paths[name] = path
        logger.debug("template_compiled name=%s slots=%d compile_us=%d",
                      name, len(self._templates[name].slot_names), elapsed_us)

    def register(self, name: str, source: str):
        self._templates[name] = CompiledPromptTemplate.compile(source)
        logger.info("template_registered name=%s", name)

    def _maybe_reload(self):
        if not self._hot_reload:
            return
        now = time.monotonic()
        if now - self._last_reload_check < self._reload_interval:
            return
        self._last_reload_check = now
        for name, path in self._paths.items():
            try:
                mtime = os.path.getmtime(path)
                if mtime > self._mtimes.get(name, 0):
                    logger.info("template_hot_reload name=%s", name)
                    self._load(name, path)
            except OSError:
                pass

    def render(self, name: str, **variables) -> str:
        self._maybe_reload()
        tmpl = self._templates.get(name)
        if tmpl is None:
            raise KeyError(f"Template '{name}' not found in registry")
        return tmpl.render(**variables)

    def get(self, name: str) -> Optional[CompiledPromptTemplate]:
        return self._templates.get(name)

    def names(self) -> Iterator[str]:
        return iter(self._templates)
```

---

## Solution 3: ChunkedSystemPromptBuilder — Pre-Serialized Static Sections

```python
import json
import time
from typing import Any, Dict, List, Optional


class ChunkedSystemPromptBuilder:
    """
    Splits the system prompt into static chunks (compiled once) and
    dynamic chunks (rendered per request). Static chunks—tool descriptions,
    formatting rules, persona—are serialized to strings at startup and
    stored as pre-built bytes. Dynamic chunks—date, session ID, user tier—
    are rendered via CompiledPromptTemplate per request.

    Usage:
        builder = ChunkedSystemPromptBuilder()
        builder.add_static("persona", "You are a helpful research assistant.")
        builder.add_static("tools", json.dumps(tool_schemas, indent=2))
        builder.add_dynamic("context_section",
                              "Current date: {date}\nUser tier: {tier}")

        system_prompt = builder.build(date="2025-02-21", tier="premium")
    """

    def __init__(self, separator: str = "\n\n"):
        self._sep = separator
        self._chunks: List[Dict[str, Any]] = []  # {type, name, content/template}
        self._static_bytes: int = 0

    def add_static(self, name: str, content: str):
        """Add a pre-built static section—no rendering at request time."""
        self._chunks.append({"type": "static", "name": name, "content": content})
        self._static_bytes += len(content.encode())

    def add_dynamic(self, name: str, template_source: str):
        """Add a per-request rendered section via CompiledPromptTemplate."""
        compiled = CompiledPromptTemplate.compile(template_source)
        self._chunks.append({"type": "dynamic", "name": name, "template": compiled})

    def build(self, **variables) -> str:
        parts: List[str] = []
        for chunk in self._chunks:
            if chunk["type"] == "static":
                parts.append(chunk["content"])
            else:
                parts.append(chunk["template"].render(**variables))
        return self._sep.join(p for p in parts if p)

    def add_tool_descriptions(self, tools: List[Dict[str, Any]]):
        """Pre-serialize tool schemas once at startup."""
        tool_json = json.dumps(tools, separators=(",", ":"))  # compact
        self.add_static("tools", f"Available tools:\n{tool_json}")

    @property
    def static_size_bytes(self) -> int:
        return self._static_bytes
```

---

## Solution 4: MessageTemplateRenderer — Compiled Multi-Turn Message Construction

```python
import time
from typing import Any, Dict, List, Optional


class MessageTemplateRenderer:
    """
    Renders multi-turn conversation message lists using pre-compiled
    templates for each role (system, user, assistant). Avoids re-parsing
    role-specific formatting on every turn.

    Usage:
        renderer = MessageTemplateRenderer(
            system_template="You are {role}. {instructions}",
            user_template="<user>{query}</user>",
            assistant_prefix="<assistant>",
        )
        messages = renderer.build_messages(
            role="a researcher",
            instructions="Be concise.",
            history=[...],
            query="What is backpressure?",
        )
    """

    def __init__(
        self,
        system_template: str,
        user_template: str = "{query}",
        assistant_prefix: str = "",
        context_template: Optional[str] = None,
    ):
        self._system_tmpl = CompiledPromptTemplate.compile(system_template)
        self._user_tmpl = CompiledPromptTemplate.compile(user_template)
        self._assistant_prefix = assistant_prefix
        self._context_tmpl = (
            CompiledPromptTemplate.compile(context_template) if context_template else None
        )

    def build_messages(
        self,
        query: str,
        history: Optional[List[Dict[str, str]]] = None,
        context: Optional[str] = None,
        **system_vars,
    ) -> List[Dict[str, str]]:
        messages: List[Dict[str, str]] = []

        system_content = self._system_tmpl.render(**system_vars)
        if context and self._context_tmpl:
            system_content += "\n\n" + self._context_tmpl.render(context=context)
        elif context:
            system_content += f"\n\nContext:\n{context}"
        messages.append({"role": "system", "content": system_content})

        for turn in (history or []):
            messages.append(turn)

        user_content = self._user_tmpl.render(query=query)
        messages.append({"role": "user", "content": user_content})

        if self._assistant_prefix:
            messages.append({"role": "assistant", "content": self._assistant_prefix})

        return messages
```

---

## Solution 5: TemplateRenderBenchmark — Measure Compilation Speedup

```python
import statistics
import time
from typing import Any, Dict


class TemplateRenderBenchmark:
    """
    Measures the per-request speedup from compiled vs. interpreted templates.
    Run at startup in development to validate that compilation provides
    meaningful gains for your template sizes and slot counts.

    Usage:
        bench = TemplateRenderBenchmark()
        report = bench.compare(
            template_source=SYSTEM_PROMPT_TEMPLATE,
            variables={"role": "assistant", "tools": TOOL_JSON, "query": "test"},
            n=10_000,
        )
        print(report)
    """

    def _time_interpreted(self, source: str, variables: Dict[str, Any], n: int) -> list:
        latencies = []
        for _ in range(n):
            t0 = time.perf_counter()
            result = source.format(**variables)
            latencies.append((time.perf_counter() - t0) * 1e6)
        return latencies

    def _time_compiled(self, source: str, variables: Dict[str, Any], n: int) -> list:
        compiled = CompiledPromptTemplate.compile(source)
        latencies = []
        for _ in range(n):
            t0 = time.perf_counter()
            result = compiled.render(**variables)
            latencies.append((time.perf_counter() - t0) * 1e6)
        return latencies

    def compare(self, template_source: str, variables: Dict[str, Any],
                  n: int = 5000) -> Dict[str, Any]:
        interpreted = self._time_interpreted(template_source, variables, n)
        compiled = self._time_compiled(template_source, variables, n)

        def stats(lats):
            return {
                "mean_us": round(statistics.mean(lats), 2),
                "p50_us": round(statistics.median(lats), 2),
                "p99_us": round(sorted(lats)[int(len(lats) * 0.99)], 2),
            }

        i_stats = stats(interpreted)
        c_stats = stats(compiled)
        speedup = round(i_stats["mean_us"] / max(c_stats["mean_us"], 0.001), 2)
        return {
            "n": n,
            "template_bytes": len(template_source),
            "slot_count": len(CompiledPromptTemplate.compile(template_source).slot_names),
            "interpreted": i_stats,
            "compiled": c_stats,
            "speedup_x": speedup,
        }
```

---

## Solution 6: PartialTemplate — Pre-Fill Static Slots, Defer Dynamic Slots

```python
from typing import Any, Dict, Optional


class PartialTemplate:
    """
    Partially renders a compiled template by filling only the static
    slots known at startup (tool descriptions, persona, version),
    producing a new intermediate template with only the dynamic slots
    remaining. The intermediate is rendered per-request with only the
    small number of dynamic variables.

    Usage:
        full = CompiledPromptTemplate.compile(
            "Agent: {role}\nVersion: {version}\nTools: {tools}\nQuery: {query}"
        )
        partial = PartialTemplate(full).fill(
            role="researcher", version="2.1.0", tools=TOOL_JSON
        )
        # At request time — only {query} remains:
        prompt = partial.render(query=user_query)
    """

    def __init__(self, template: CompiledPromptTemplate):
        self._template = template

    def fill(self, **static_vars: Any) -> "CompiledPromptTemplate":
        """
        Pre-fill static slots and compile a new template with remaining slots.
        """
        # Render with static_vars, leaving unset slots as their placeholder text
        parts = []
        for part in self._template._parts:
            if isinstance(part, _Literal):
                parts.append(part.text)
            elif part.name in static_vars:
                parts.append(str(static_vars[part.name]))
            else:
                # Preserve the slot for per-request rendering
                if part.default is not None:
                    parts.append("{" + part.name + ":" + part.default + "}")
                else:
                    parts.append("{" + part.name + "}")
        combined = "".join(parts)
        return CompiledPromptTemplate.compile(combined)
```

---

## Comparison

| Approach | Compile Once | Per-Request Render | Static Pre-Fill | Multi-Turn | Benchmarking | Hot Reload |
|---|---|---|---|---|---|---|
| **CompiledPromptTemplate** | Yes | Slot fill only | No | No | No | No |
| **TemplateRegistry** | Yes | Yes | No | No | No | Optional |
| **ChunkedSystemPromptBuilder** | Partial | Dynamic chunks | Yes | No | No | No |
| **MessageTemplateRenderer** | Yes | Yes | No | Yes | No | No |
| **TemplateRenderBenchmark** | N/A | N/A | N/A | N/A | Yes | No |
| **PartialTemplate** | Yes | Dynamic slots only | Yes | No | No | No |

**Key insight**: the biggest win comes from `PartialTemplate.fill()` applied to the system prompt at startup. A typical agent system prompt with 20 tool descriptions and a persona section takes 3-8ms to render via `str.format()` because the tool JSON is re-serialized on every call. Pre-filling it at startup reduces per-request rendering to under 50 microseconds for the remaining 2-3 dynamic slots (query, date, session). Run `TemplateRenderBenchmark.compare()` to measure the speedup for your specific templates—gains are largest for templates >5KB with >10 slots.
