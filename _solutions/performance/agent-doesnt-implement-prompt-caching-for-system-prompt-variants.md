---
title: "Agent Doesn't Implement Prompt Caching for System Prompt Variants"
description: "Agents that serve multiple user tiers, locales, or feature flags maintain dozens of system prompt variants but compute fresh KV cache entries for each variant on every request. Implement a prompt variant cache that pre-renders each distinct system prompt variant into a stable prefix, warms it proactively, and routes each request to its pre-cached variant — eliminating redundant prefix computation across requests that share the same system prompt."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-prompt-caching-for-system-prompt-variants
tags: [prompt-caching, system-prompt-variants, kv-cache, variant-routing, feature-flags, multi-tenant-caching]
symptoms:
  - "Free-tier and premium-tier system prompts re-computed from scratch on every request"
  - "Locale-specific system prompt variants each incur full prefix computation cost"
  - "Feature-flagged system prompt changes invalidate cache for all users simultaneously"
  - "No pre-warming of new system prompt variants before they are rolled out"
  - "Variant selection logic embedded deep in prompt assembly — no caching hook"
---

## Why This Happens

Agents with multiple user tiers, locales, or A/B test variants need different system prompts per user segment. Each unique system prompt prefix occupies a separate KV cache entry in the LLM API. Without explicit variant management, these entries are populated lazily on the first request for each variant and expire if not hit within the cache TTL. Proactive variant cache management pre-registers all active variants, warms them before traffic arrives, monitors their cache status, and ensures that variant rollouts are staged so new cache entries are warm before old ones are invalidated.

## Solution 1: System Prompt Variant

```python
import hashlib
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class SystemPromptVariant:
    variant_id: str
    display_name: str
    content: str
    applicable_tiers: List[str] = field(default_factory=list)   # e.g. ["free", "standard"]
    applicable_locales: List[str] = field(default_factory=list) # e.g. ["en", "fr"]
    feature_flags: Dict[str, Any] = field(default_factory=dict)
    is_active: bool = True
    created_at: float = field(default_factory=time.time)

    def fingerprint(self) -> str:
        """Stable content hash used as the KV cache key prefix."""
        return hashlib.sha256(self.content.encode()).hexdigest()[:32]

    def token_estimate(self, chars_per_token: float = 4.0) -> int:
        return max(1, int(len(self.content) / chars_per_token))
```

## Solution 2: Variant Selector

```python
from typing import Dict, List, Optional


class SystemPromptVariantSelector:
    """
    Selects the appropriate system prompt variant for a given request context.
    Falls back to the default variant if no specific match is found.
    """

    def __init__(
        self,
        variants: List[SystemPromptVariant],
        default_variant_id: str,
    ):
        self._variants = {v.variant_id: v for v in variants if v.is_active}
        self._default_id = default_variant_id

    def select(
        self,
        tier: str = "",
        locale: str = "",
        feature_flags: Optional[Dict[str, Any]] = None,
    ) -> SystemPromptVariant:
        feature_flags = feature_flags or {}
        candidates = []

        for variant in self._variants.values():
            score = 0
            if variant.applicable_tiers and tier not in variant.applicable_tiers:
                continue
            if variant.applicable_locales and locale not in variant.applicable_locales:
                continue
            for flag, val in variant.feature_flags.items():
                if feature_flags.get(flag) == val:
                    score += 1
                elif flag in feature_flags:
                    score -= 1

            candidates.append((score, variant))

        if not candidates:
            return self._variants.get(self._default_id) or next(iter(self._variants.values()))

        return max(candidates, key=lambda x: x[0])[1]

    def all_active_variants(self) -> List[SystemPromptVariant]:
        return list(self._variants.values())
```

## Solution 3: Variant Cache Warmer

```python
import asyncio
import time
from typing import Any, Callable, Dict, List, Optional


class VariantCacheWarmStatus:
    def __init__(self, variant_id: str):
        self.variant_id = variant_id
        self.warmed_at: Optional[float] = None
        self.last_warmed_at: Optional[float] = None
        self.warm_count: int = 0
        self.is_warm: bool = False

    def mark_warm(self) -> None:
        now = time.time()
        if self.warmed_at is None:
            self.warmed_at = now
        self.last_warmed_at = now
        self.warm_count += 1
        self.is_warm = True

    def age_s(self) -> Optional[float]:
        if self.last_warmed_at is None:
            return None
        return round(time.time() - self.last_warmed_at, 1)


class SystemPromptVariantCacheWarmer:
    """
    Pre-warms KV cache entries for all active system prompt variants
    by sending minimal no-op requests to the LLM API.
    Tracks warm status per variant and re-warms on expiry.
    """

    def __init__(
        self,
        selector: SystemPromptVariantSelector,
        llm_fn: Callable,        # async (system_prompt, max_tokens=1) -> None
        cache_ttl_s: float = 300.0,
        warm_interval_s: float = 240.0,
    ):
        self._selector = selector
        self._llm_fn = llm_fn
        self._ttl = cache_ttl_s
        self._interval = warm_interval_s
        self._status: Dict[str, VariantCacheWarmStatus] = {}

    def _needs_warming(self, variant_id: str) -> bool:
        status = self._status.get(variant_id)
        if status is None or not status.is_warm:
            return True
        age = status.age_s()
        return age is not None and age > self._interval

    async def warm_variant(self, variant: SystemPromptVariant) -> bool:
        try:
            await self._llm_fn(variant.content, 1)
            if variant.variant_id not in self._status:
                self._status[variant.variant_id] = VariantCacheWarmStatus(variant.variant_id)
            self._status[variant.variant_id].mark_warm()
            return True
        except Exception:
            return False

    async def warm_all(self) -> dict:
        variants = self._selector.all_active_variants()
        results = {"total": len(variants), "warmed": 0, "skipped": 0, "failed": 0}
        for variant in variants:
            if not self._needs_warming(variant.variant_id):
                results["skipped"] += 1
                continue
            success = await self.warm_variant(variant)
            if success:
                results["warmed"] += 1
            else:
                results["failed"] += 1
        return results

    async def run_periodic(self, poll_interval_s: float = 60.0) -> None:
        while True:
            await asyncio.sleep(poll_interval_s)
            await self.warm_all()

    def warm_status_summary(self) -> dict:
        return {
            vid: {
                "is_warm": s.is_warm,
                "age_s": s.age_s(),
                "warm_count": s.warm_count,
            }
            for vid, s in self._status.items()
        }
```

## Solution 4: Cache-Aware Variant Router

```python
import time
from typing import Any, Dict, Optional


class CacheAwareVariantRouter:
    """
    Routes each request to its system prompt variant and tracks
    which variants are receiving traffic (for cache priority decisions).
    """

    def __init__(
        self,
        selector: SystemPromptVariantSelector,
        warmer: SystemPromptVariantCacheWarmer,
    ):
        self._selector = selector
        self._warmer = warmer
        self._traffic: Dict[str, int] = {}
        self._total_requests = 0

    def route(
        self,
        tier: str = "",
        locale: str = "",
        feature_flags: Optional[Dict[str, Any]] = None,
    ) -> SystemPromptVariant:
        variant = self._selector.select(tier, locale, feature_flags)
        self._traffic[variant.variant_id] = self._traffic.get(variant.variant_id, 0) + 1
        self._total_requests += 1
        return variant

    def cache_miss_risk(self, variant_id: str) -> float:
        """Returns 0.0–1.0 estimate of probability that variant cache is stale."""
        status = self._warmer._status.get(variant_id)
        if not status or not status.is_warm:
            return 1.0
        age = status.age_s() or 0.0
        return round(min(age / max(self._warmer._ttl, 1), 1.0), 4)

    def traffic_distribution(self) -> dict:
        total = max(self._total_requests, 1)
        return {
            vid: round(count / total, 4)
            for vid, count in self._traffic.items()
        }
```

## Solution 5: Variant Rollout Manager

```python
import time
from typing import Callable, List, Optional


class VariantRolloutManager:
    """
    Manages staged rollout of new system prompt variants.
    Pre-warms the new variant before activating it and supports
    rollback to the previous variant on error.
    """

    def __init__(
        self,
        warmer: SystemPromptVariantCacheWarmer,
        on_activate: Callable[[str], None],   # callback(variant_id)
        on_rollback: Callable[[str], None],
    ):
        self._warmer = warmer
        self._on_activate = on_activate
        self._on_rollback = on_rollback
        self._rollout_log: List[dict] = []

    async def rollout(
        self,
        new_variant: SystemPromptVariant,
        warm_before_activate: bool = True,
    ) -> dict:
        record = {
            "variant_id": new_variant.variant_id,
            "started_at": time.time(),
            "warmed": False,
            "activated": False,
        }

        if warm_before_activate:
            success = await self._warmer.warm_variant(new_variant)
            record["warmed"] = success
            if not success:
                record["error"] = "pre-warm failed"
                self._rollout_log.append(record)
                return record

        try:
            self._on_activate(new_variant.variant_id)
            record["activated"] = True
        except Exception as exc:
            record["error"] = str(exc)[:200]
            try:
                self._on_rollback(new_variant.variant_id)
            except Exception:
                pass

        self._rollout_log.append(record)
        return record
```

## Solution 6: Prompt Variant Caching Dashboard

```python
import time


class PromptVariantCachingDashboard:
    """
    Combines variant warm status, traffic distribution, and rollout log.
    """

    def __init__(
        self,
        router: CacheAwareVariantRouter,
        warmer: SystemPromptVariantCacheWarmer,
        rollout_manager: VariantRolloutManager,
    ):
        self._router = router
        self._warmer = warmer
        self._rollout = rollout_manager

    def render(self) -> dict:
        warm_status = self._warmer.warm_status_summary()
        traffic = self._router.traffic_distribution()
        miss_risks = {
            vid: self._router.cache_miss_risk(vid)
            for vid in warm_status
        }
        return {
            "generated_at": time.time(),
            "variants": {
                vid: {
                    "warm": warm_status[vid],
                    "traffic_fraction": traffic.get(vid, 0.0),
                    "cache_miss_risk": miss_risks.get(vid, 1.0),
                }
                for vid in warm_status
            },
            "total_requests": self._router._total_requests,
            "recent_rollouts": self._rollout._rollout_log[-5:],
        }
```

## Comparison

| Approach | Variant Selection | Cache Warming | Traffic Tracking | Staged Rollout | Dashboard |
|---|---|---|---|---|---|
| SystemPromptVariantSelector | Yes (tier+locale+flags) | No | No | No | No |
| SystemPromptVariantCacheWarmer | No | Yes (periodic) | No | No | No |
| CacheAwareVariantRouter | Via selector | Via warmer | Yes | No | No |
| VariantRolloutManager | No | Via warmer | No | Yes | No |
| PromptVariantCachingDashboard | No | No | Via router | Via manager | Yes |

**Best for production**: Pre-warm all variants 60 seconds before each hour boundary — LLM provider caches often expire on the hour and a cold first request of the hour will pay full prefix computation cost. Set `warm_interval_s = cache_ttl_s * 0.8` to re-warm before the cache expires rather than after. Monitor `cache_miss_risk` per variant — a high-traffic variant with elevated miss risk means the warming interval is too long. Use `VariantRolloutManager` for any system prompt change affecting more than 10% of traffic; unexpected behavior in the new prompt is much easier to roll back when the old cache entry is still warm.
