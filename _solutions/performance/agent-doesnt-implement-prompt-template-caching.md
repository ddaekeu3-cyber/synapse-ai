---
title: "Agent Doesn't Implement Prompt Template Caching"
description: "Agents that recompile prompt templates on every request waste CPU and introduce variable latency from repeated string formatting, Jinja rendering, or token counting operations. Implement prompt template caching that stores compiled templates and their rendered variants keyed by parameter fingerprints, with cache invalidation on template changes and hit-rate monitoring."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-prompt-template-caching
tags: [prompt-caching, template-compilation, render-cache, token-counting-cache, prompt-performance, jinja-cache]
symptoms:
  - "Same prompt template is re-parsed and re-compiled on every LLM call"
  - "Token counting runs on every request even when the prompt base is unchanged"
  - "Variable latency in prompt assembly phase proportional to template complexity"
  - "No reuse of rendered prompts for repeated calls with identical parameters"
  - "Profiling shows 5-15ms per request spent in template rendering before the LLM call"
---

## Why This Happens

Prompt templates — whether plain f-strings, Jinja2 templates, or structured message builders — involve parsing, validation, and string rendering work that is proportional to template complexity. When agents call `template.render(...)` inside the hot path of every request, the same parse tree is reconstructed and the same substitutions are applied. For templates with stable base content and frequently repeated parameter combinations (e.g., same system prompt + same tool schema), caching the compiled template and the rendered output eliminates redundant work and makes latency predictable.

## Solution 1: Compiled Prompt Template

```python
import hashlib
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class CompiledPromptTemplate:
    template_id: str
    source: str                    # raw template string
    compiled_at: float = field(default_factory=time.time)
    render_count: int = 0
    cache_hits: int = 0
    compiled_object: Any = None    # the parsed/compiled template object

    def fingerprint(self) -> str:
        return hashlib.sha256(self.source.encode()).hexdigest()[:16]
```

## Solution 2: Template Compiler Cache

```python
import hashlib
import time
from threading import Lock
from typing import Any, Callable, Dict, Optional


class TemplateCompilerCache:
    """
    Caches compiled template objects (e.g., Jinja2 Template instances)
    keyed by template source fingerprint. Re-compiles only when the
    source changes. Thread-safe for concurrent request handling.
    """

    def __init__(
        self,
        compile_fn: Callable[[str], Any],
        max_templates: int = 500,
    ):
        self._compile = compile_fn
        self._max = max_templates
        self._cache: Dict[str, CompiledPromptTemplate] = {}
        self._lock = Lock()

    def get_or_compile(self, template_id: str, source: str) -> CompiledPromptTemplate:
        fp = hashlib.sha256(source.encode()).hexdigest()[:16]
        cache_key = f"{template_id}:{fp}"

        with self._lock:
            if cache_key in self._cache:
                entry = self._cache[cache_key]
                entry.cache_hits += 1
                return entry

            if len(self._cache) >= self._max:
                self._evict_lru()

            compiled_obj = self._compile(source)
            entry = CompiledPromptTemplate(
                template_id=template_id,
                source=source,
                compiled_object=compiled_obj,
            )
            self._cache[cache_key] = entry
            return entry

    def _evict_lru(self) -> None:
        if not self._cache:
            return
        lru_key = min(
            self._cache,
            key=lambda k: self._cache[k].compiled_at,
        )
        del self._cache[lru_key]

    def stats(self) -> dict:
        with self._lock:
            total_hits = sum(e.cache_hits for e in self._cache.values())
            return {
                "cached_templates": len(self._cache),
                "total_compile_cache_hits": total_hits,
            }
```

## Solution 3: Rendered Prompt Cache

```python
import hashlib
import json
import time
from threading import Lock
from typing import Any, Dict, Optional, Tuple


class RenderedPromptCache:
    """
    Caches fully rendered prompt strings keyed by (template_id, parameter_hash).
    Eliminates re-rendering for repeated calls with identical parameters.
    """

    def __init__(
        self,
        max_entries: int = 2000,
        ttl_seconds: float = 300.0,
    ):
        self._max = max_entries
        self._ttl = ttl_seconds
        self._cache: Dict[str, dict] = {}
        self._access_times: Dict[str, float] = {}
        self._lock = Lock()
        self._hits = 0
        self._misses = 0

    def _param_hash(self, params: Dict[str, Any]) -> str:
        serialized = json.dumps(params, sort_keys=True, default=str)
        return hashlib.sha256(serialized.encode()).hexdigest()[:16]

    def _key(self, template_id: str, params: Dict[str, Any]) -> str:
        return f"{template_id}:{self._param_hash(params)}"

    def get(
        self, template_id: str, params: Dict[str, Any]
    ) -> Optional[str]:
        key = self._key(template_id, params)
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                self._misses += 1
                return None
            if time.time() - entry["stored_at"] > self._ttl:
                del self._cache[key]
                self._access_times.pop(key, None)
                self._misses += 1
                return None
            self._access_times[key] = time.time()
            self._hits += 1
            return entry["rendered"]

    def set(
        self,
        template_id: str,
        params: Dict[str, Any],
        rendered: str,
    ) -> None:
        key = self._key(template_id, params)
        with self._lock:
            if len(self._cache) >= self._max:
                self._evict_lru()
            self._cache[key] = {"rendered": rendered, "stored_at": time.time()}
            self._access_times[key] = time.time()

    def _evict_lru(self) -> None:
        if not self._access_times:
            return
        lru = min(self._access_times, key=self._access_times.get)
        self._cache.pop(lru, None)
        self._access_times.pop(lru, None)

    def hit_rate(self) -> float:
        total = self._hits + self._misses
        return round(self._hits / max(total, 1), 4)

    def stats(self) -> dict:
        return {
            "entries": len(self._cache),
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": self.hit_rate(),
        }
```

## Solution 4: Token Count Cache

```python
import hashlib
from threading import Lock
from typing import Callable, Dict, Optional


class TokenCountCache:
    """
    Caches token counts for rendered prompt strings.
    Token counting is typically 1-3ms per call; caching eliminates
    redundant counting for repeated identical prompts.
    """

    def __init__(
        self,
        count_fn: Callable[[str], int],
        max_entries: int = 5000,
    ):
        self._count_fn = count_fn
        self._max = max_entries
        self._cache: Dict[str, int] = {}
        self._lock = Lock()
        self._hits = 0
        self._misses = 0

    def count(self, text: str) -> int:
        key = hashlib.sha256(text.encode()).hexdigest()[:20]
        with self._lock:
            if key in self._cache:
                self._hits += 1
                return self._cache[key]
        count = self._count_fn(text)
        with self._lock:
            if len(self._cache) >= self._max:
                first_key = next(iter(self._cache))
                del self._cache[first_key]
            self._cache[key] = count
            self._misses += 1
        return count

    def stats(self) -> dict:
        total = self._hits + self._misses
        return {
            "entries": len(self._cache),
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": round(self._hits / max(total, 1), 4),
        }
```

## Solution 5: Cached Prompt Builder

```python
import time
from typing import Any, Callable, Dict, List, Optional


class CachedPromptBuilder:
    """
    Combines template compilation caching, render caching, and
    token count caching into a single prompt assembly interface.
    """

    def __init__(
        self,
        compiler_cache: TemplateCompilerCache,
        render_cache: RenderedPromptCache,
        token_cache: TokenCountCache,
    ):
        self._compiler = compiler_cache
        self._render = render_cache
        self._tokens = token_cache
        self._build_times_ms: List[float] = []

    def build(
        self,
        template_id: str,
        source: str,
        params: Dict[str, Any],
    ) -> dict:
        start = time.time()

        # Check render cache first
        cached_render = self._render.get(template_id, params)
        if cached_render is not None:
            token_count = self._tokens.count(cached_render)
            elapsed_ms = round((time.time() - start) * 1000, 3)
            self._build_times_ms.append(elapsed_ms)
            return {
                "rendered": cached_render,
                "token_count": token_count,
                "cache_hit": True,
                "build_ms": elapsed_ms,
            }

        # Compile template (cached)
        compiled = self._compiler.get_or_compile(template_id, source)
        compiled.render_count += 1

        # Render
        rendered = compiled.compiled_object.render(**params)

        # Store in render cache
        self._render.set(template_id, params, rendered)

        token_count = self._tokens.count(rendered)
        elapsed_ms = round((time.time() - start) * 1000, 3)
        self._build_times_ms.append(elapsed_ms)

        return {
            "rendered": rendered,
            "token_count": token_count,
            "cache_hit": False,
            "build_ms": elapsed_ms,
        }

    def avg_build_ms(self) -> float:
        if not self._build_times_ms:
            return 0.0
        recent = self._build_times_ms[-1000:]
        return round(sum(recent) / len(recent), 3)
```

## Solution 6: Prompt Template Cache Dashboard

```python
import time


class PromptTemplateCacheDashboard:
    """
    Surfaces hit rates, latency savings, and cache utilization
    across all three caching layers.
    """

    def __init__(
        self,
        compiler_cache: TemplateCompilerCache,
        render_cache: RenderedPromptCache,
        token_cache: TokenCountCache,
        builder: CachedPromptBuilder,
    ):
        self._compiler = compiler_cache
        self._render = render_cache
        self._tokens = token_cache
        self._builder = builder

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "compiler_cache": self._compiler.stats(),
            "render_cache": self._render.stats(),
            "token_count_cache": self._tokens.stats(),
            "avg_build_ms": self._builder.avg_build_ms(),
        }
```

## Comparison

| Approach | Compile Cache | Render Cache | Token Count Cache | Hit Rate Tracking | Dashboard |
|---|---|---|---|---|---|
| TemplateCompilerCache | Yes (by fingerprint) | No | No | Partial | No |
| RenderedPromptCache | No | Yes (TTL+LRU) | No | Yes | No |
| TokenCountCache | No | No | Yes (hash) | Yes | No |
| CachedPromptBuilder | Via compiler | Via render | Via token | No | No |
| PromptTemplateCacheDashboard | No | No | No | No | Yes |

**Best for production**: The render cache provides the most latency savings — a cache hit skips both compilation and rendering. Set `ttl_seconds=300` to match the typical session length so cached renders stay valid for the duration of a conversation but expire before stale parameter values could cause incorrect outputs. Use `TokenCountCache` whenever token counting is done via a local tokenizer (tiktoken, sentencepiece) — these are fast but not free, and the same rendered prompt is counted dozens of times in multi-turn conversations. Monitor `hit_rate` via the dashboard: render cache hit rates below 30% indicate that parameter combinations are too diverse for caching to help, and the cache size or TTL should be reconsidered.
