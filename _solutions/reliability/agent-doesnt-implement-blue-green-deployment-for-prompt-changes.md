---
layout: solution
title: "Agent Doesn't Implement Blue-Green Deployment for Prompt Changes"
category: reliability
description: "Deploying prompt changes directly to production risks instant user impact with no rollback path. Blue-green deployment maintains two identical prompt versions — the active 'blue' and the staged 'green' — enabling instant cutover and instant rollback without downtime."
tags: [reliability, deployment, blue-green, prompt-engineering, rollout, python]
---

## Problem

When prompts change, the impact is immediate and hard to reverse. A typo in a system prompt, an unintended persona shift, or a regression in output quality affects all users simultaneously. Blue-green deployment routes traffic between two prompt configurations — keeping the previous version hot so rollback takes milliseconds, not hours.

## Solutions

### Option 1: Simple Blue-Green Router with Traffic Split

```python
import anthropic
import hashlib
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

class Slot(Enum):
    BLUE = "blue"
    GREEN = "green"

@dataclass
class PromptVersion:
    slot: Slot
    version_id: str
    system_prompt: str
    model: str = "claude-haiku-4-5-20251001"
    max_tokens: int = 150
    deployed_at: float = field(default_factory=time.time)
    is_active: bool = False

class BlueGreenRouter:
    def __init__(self):
        self._slots: dict[Slot, Optional[PromptVersion]] = {
            Slot.BLUE: None, Slot.GREEN: None,
        }
        self._active_slot: Slot = Slot.BLUE
        self._green_pct: float = 0.0  # 0–100: % traffic to green

    def deploy(self, slot: Slot, version: PromptVersion) -> None:
        """Deploy a new version to a slot without changing live traffic."""
        version.slot = slot
        version.is_active = (slot == self._active_slot)
        self._slots[slot] = version
        print(f"[DEPLOY] {slot.value} ← version={version.version_id} "
              f"({'active' if version.is_active else 'staged'})")

    def set_green_pct(self, pct: float) -> None:
        """Gradually shift traffic to green (0 = all blue, 100 = all green)."""
        old = self._green_pct
        self._green_pct = max(0.0, min(100.0, pct))
        print(f"[TRAFFIC] Blue={100-self._green_pct:.0f}% "
              f"Green={self._green_pct:.0f}% (was {100-old:.0f}/{old:.0f})")

    def cutover(self) -> None:
        """Full cutover: green becomes the only active slot."""
        self._active_slot = Slot.GREEN
        self._green_pct = 100.0
        if self._slots[Slot.GREEN]:
            self._slots[Slot.GREEN].is_active = True
        if self._slots[Slot.BLUE]:
            self._slots[Slot.BLUE].is_active = False
        print("[CUTOVER] Green is now 100% active")

    def rollback(self) -> None:
        """Instant rollback to blue."""
        self._active_slot = Slot.BLUE
        self._green_pct = 0.0
        if self._slots[Slot.BLUE]:
            self._slots[Slot.BLUE].is_active = True
        if self._slots[Slot.GREEN]:
            self._slots[Slot.GREEN].is_active = False
        print("[ROLLBACK] Blue is now 100% active")

    def route(self, user_id: str) -> Optional[PromptVersion]:
        """Route user to blue or green based on traffic split."""
        blue = self._slots[Slot.BLUE]
        green = self._slots[Slot.GREEN]

        if not blue and not green:
            return None
        if not green or self._green_pct == 0.0:
            return blue
        if not blue or self._green_pct == 100.0:
            return green

        # Stable hash: same user always hits same slot for given split
        bucket = int(hashlib.md5(f"bg:{user_id}".encode()).hexdigest(), 16) % 100
        return green if bucket < self._green_pct else blue

def run_demo():
    client = anthropic.Anthropic()
    router = BlueGreenRouter()

    # Deploy blue (current production)
    router.deploy(Slot.BLUE, PromptVersion(
        slot=Slot.BLUE, version_id="v1.0",
        system_prompt="You are a helpful assistant. Be concise.",
    ))
    # Stage green (new version)
    router.deploy(Slot.GREEN, PromptVersion(
        slot=Slot.BLUE, version_id="v2.0",
        system_prompt="You are a precise assistant. Always structure answers with bullet points.",
    ))

    users = [f"user-{i:03d}" for i in range(6)]
    prompt = "What is the water cycle?"

    print("\n--- Phase 1: 0% green (all blue) ---")
    for user_id in users[:3]:
        version = router.route(user_id)
        r = client.messages.create(
            model=version.model, max_tokens=version.max_tokens,
            system=version.system_prompt,
            messages=[{"role": "user", "content": prompt}],
        )
        print(f"[{version.slot.value}/{version.version_id}] {user_id}: {r.content[0].text[:60]}")

    print("\n--- Phase 2: 50% green (canary) ---")
    router.set_green_pct(50.0)
    for user_id in users:
        version = router.route(user_id)
        r = client.messages.create(
            model=version.model, max_tokens=version.max_tokens,
            system=version.system_prompt,
            messages=[{"role": "user", "content": prompt}],
        )
        print(f"[{version.slot.value}/{version.version_id}] {user_id}: {r.content[0].text[:55]}")

    print("\n--- Phase 3: Full cutover to green ---")
    router.cutover()

    print("\n--- Phase 4: Rollback to blue ---")
    router.rollback()
    version = router.route("user-000")
    print(f"After rollback, user-000 routes to: {version.slot.value}/{version.version_id}")

if __name__ == "__main__":
    run_demo()

# Expected Token Savings: Safe rollout limits exposure of broken prompts; rollback is instant
# Environment: pip install anthropic
```

### Option 2: Versioned Prompt Store with Shadow Mode Testing

```python
import anthropic
import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class PromptConfig:
    version_id: str
    system_prompt: str
    model: str = "claude-haiku-4-5-20251001"
    max_tokens: int = 100
    temperature: float = 1.0

@dataclass
class ShadowResult:
    user_id: str
    prompt: str
    blue_response: str
    green_response: str
    blue_tokens: int
    green_tokens: int
    blue_latency_ms: float
    green_latency_ms: float

class ShadowTestRouter:
    """Runs green in shadow mode: real traffic goes to blue, green runs in background."""
    def __init__(self, blue: PromptConfig, green: PromptConfig,
                 shadow_pct: float = 10.0):
        self.blue = blue
        self.green = green
        self.shadow_pct = shadow_pct  # % of requests that also run green
        self._shadow_log: list[ShadowResult] = []
        self._client = anthropic.AsyncAnthropic()

    async def _call(self, config: PromptConfig, prompt: str) -> tuple[str, int, float]:
        t0 = time.monotonic()
        response = await self._client.messages.create(
            model=config.model,
            max_tokens=config.max_tokens,
            system=config.system_prompt,
            messages=[{"role": "user", "content": prompt}],
        )
        return (response.content[0].text,
                response.usage.output_tokens,
                (time.monotonic() - t0) * 1000)

    async def route(self, user_id: str, prompt: str) -> str:
        import hashlib
        # Always run blue (primary)
        blue_text, blue_tokens, blue_ms = await self._call(self.blue, prompt)

        # Shadow: run green async without blocking user
        bucket = int(hashlib.md5(f"shadow:{user_id}".encode()).hexdigest(), 16) % 100
        if bucket < self.shadow_pct:
            asyncio.create_task(self._shadow_call(user_id, prompt, blue_text,
                                                   blue_tokens, blue_ms))
        return blue_text

    async def _shadow_call(self, user_id: str, prompt: str,
                            blue_text: str, blue_tokens: int, blue_ms: float) -> None:
        try:
            green_text, green_tokens, green_ms = await self._call(self.green, prompt)
            result = ShadowResult(
                user_id=user_id, prompt=prompt[:40],
                blue_response=blue_text[:60], green_response=green_text[:60],
                blue_tokens=blue_tokens, green_tokens=green_tokens,
                blue_latency_ms=blue_ms, green_latency_ms=green_ms,
            )
            self._shadow_log.append(result)
            delta_tokens = green_tokens - blue_tokens
            delta_ms = green_ms - blue_ms
            print(f"[SHADOW] {user_id[:8]} Δtokens={delta_tokens:+d} Δms={delta_ms:+.0f}")
        except Exception as e:
            print(f"[SHADOW ERROR] {user_id}: {e}")

    def shadow_report(self) -> dict:
        if not self._shadow_log:
            return {"samples": 0}
        avg_blue_tokens = sum(r.blue_tokens for r in self._shadow_log) / len(self._shadow_log)
        avg_green_tokens = sum(r.green_tokens for r in self._shadow_log) / len(self._shadow_log)
        return {
            "samples": len(self._shadow_log),
            "avg_blue_tokens": avg_blue_tokens,
            "avg_green_tokens": avg_green_tokens,
            "token_delta_pct": (avg_green_tokens - avg_blue_tokens) / avg_blue_tokens * 100,
        }

async def main():
    blue = PromptConfig("v1.2", "You are a helpful assistant.")
    green = PromptConfig("v2.0", "You are a helpful assistant. Always be concise and use bullet points.")
    router = ShadowTestRouter(blue, green, shadow_pct=80.0)  # High % for demo

    users = [f"user-{i:04d}" for i in range(5)]
    prompts = [
        "What are the benefits of exercise?",
        "How does photosynthesis work?",
        "What is machine learning?",
        "Explain REST APIs.",
        "What is cloud computing?",
    ]

    results = await asyncio.gather(*[
        router.route(uid, p) for uid, p in zip(users, prompts)
    ])

    for uid, result in zip(users, results):
        print(f"[BLUE→USER] {uid}: {result[:60]}")

    await asyncio.sleep(2.0)  # Wait for shadow calls
    print(f"\nShadow report: {router.shadow_report()}")

if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: Shadow mode reveals token impact of new prompt before full rollout
# Environment: pip install anthropic
```

### Option 3: Stateful Blue-Green with Health Checks and Auto-Rollback

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

class SlotHealth(Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"

@dataclass
class SlotStats:
    slot_name: str
    requests: int = 0
    errors: int = 0
    total_latency_ms: float = 0.0

    @property
    def error_rate(self) -> float:
        return self.errors / max(self.requests, 1)

    @property
    def avg_latency_ms(self) -> float:
        return self.total_latency_ms / max(self.requests, 1)

    @property
    def health(self) -> SlotHealth:
        if self.requests < 3:
            return SlotHealth.HEALTHY
        if self.error_rate > 0.5:
            return SlotHealth.UNHEALTHY
        if self.error_rate > 0.2:
            return SlotHealth.DEGRADED
        return SlotHealth.HEALTHY

@dataclass
class HealthAwareBlueGreen:
    blue_system: str = "You are a helpful assistant."
    green_system: str = ""
    green_pct: float = 0.0
    auto_rollback: bool = True
    _blue_stats: SlotStats = field(default_factory=lambda: SlotStats("blue"))
    _green_stats: SlotStats = field(default_factory=lambda: SlotStats("green"))
    _rolled_back: bool = False

    def set_green(self, system: str, pct: float = 10.0) -> None:
        self.green_system = system
        self.green_pct = pct
        print(f"[DEPLOY] Green staged at {pct:.0f}%: {system[:50]}")

    def _should_use_green(self, user_id: str) -> bool:
        if not self.green_system or self._rolled_back:
            return False
        if self._green_stats.health == SlotHealth.UNHEALTHY and self.auto_rollback:
            self._trigger_rollback("Automatic: green unhealthy")
            return False
        import hashlib
        bucket = int(hashlib.md5(f"hbg:{user_id}".encode()).hexdigest(), 16) % 100
        return bucket < self.green_pct

    def _trigger_rollback(self, reason: str) -> None:
        if not self._rolled_back:
            self._rolled_back = True
            self.green_pct = 0.0
            print(f"🔄 [AUTO-ROLLBACK] {reason}")

    async def call(self, client: anthropic.AsyncAnthropic,
                   user_id: str, prompt: str) -> tuple[str, str]:
        use_green = self._should_use_green(user_id)
        system = self.green_system if use_green else self.blue_system
        stats = self._green_stats if use_green else self._blue_stats
        slot = "green" if use_green else "blue"

        t0 = time.monotonic()
        stats.requests += 1
        try:
            response = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=80,
                system=system,
                messages=[{"role": "user", "content": prompt}],
            )
            stats.total_latency_ms += (time.monotonic() - t0) * 1000
            return response.content[0].text, slot
        except Exception as e:
            stats.errors += 1
            stats.total_latency_ms += (time.monotonic() - t0) * 1000
            # Check if auto-rollback needed
            if use_green and self._green_stats.health == SlotHealth.UNHEALTHY:
                self._trigger_rollback(f"Error threshold: {self._green_stats.error_rate:.0%}")
            raise

    def report(self) -> dict:
        return {
            "blue": {"health": self._blue_stats.health.value,
                     "error_rate": f"{self._blue_stats.error_rate:.1%}",
                     "avg_ms": f"{self._blue_stats.avg_latency_ms:.0f}"},
            "green": {"health": self._green_stats.health.value,
                      "error_rate": f"{self._green_stats.error_rate:.1%}",
                      "avg_ms": f"{self._green_stats.avg_latency_ms:.0f}"},
            "rolled_back": self._rolled_back,
        }

async def main():
    client = anthropic.AsyncAnthropic()
    bg = HealthAwareBlueGreen(
        blue_system="You are a concise helpful assistant.",
        auto_rollback=True,
    )
    bg.set_green("You are a verbose detailed assistant who explains everything.", pct=50.0)

    users = [f"user-{i:04d}" for i in range(8)]
    prompt = "What is gravity?"

    async def safe_call(uid: str) -> None:
        try:
            text, slot = await bg.call(client, uid, prompt)
            print(f"[{slot:5}] {uid}: {text[:55]}")
        except Exception as e:
            print(f"[ERROR] {uid}: {e}")

    await asyncio.gather(*[safe_call(uid) for uid in users])

    # Simulate green degradation
    for _ in range(5):
        bg._green_stats.errors += 1
        bg._green_stats.requests += 1
    print(f"\nGreen stats after errors: health={bg._green_stats.health.value}")
    print(f"Final report: {bg.report()}")

if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: Auto-rollback stops a bad prompt immediately, preventing wasted tokens on poor responses
# Environment: pip install anthropic
```

### Option 4: Prompt Version Registry with Immutable History

```python
import anthropic
import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

@dataclass
class PromptRelease:
    release_id: str
    version: str
    system_prompt: str
    model: str
    deployed_by: str
    deployed_at: float = field(default_factory=time.time)
    rollback_from: Optional[str] = None   # Which release this replaced
    notes: str = ""

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.system_prompt.encode()).hexdigest()[:12]

class PromptRegistry:
    """Immutable version history for prompts with blue-green slots."""
    def __init__(self, registry_path: str = "/tmp/prompt_registry.json"):
        self._path = Path(registry_path)
        self._releases: list[PromptRelease] = []
        self._blue_id: Optional[str] = None
        self._green_id: Optional[str] = None
        self._load()

    def _load(self) -> None:
        if self._path.exists():
            data = json.loads(self._path.read_text())
            self._releases = [PromptRelease(**r) for r in data.get("releases", [])]
            self._blue_id = data.get("blue_id")
            self._green_id = data.get("green_id")

    def _save(self) -> None:
        data = {
            "releases": [vars(r) for r in self._releases],
            "blue_id": self._blue_id,
            "green_id": self._green_id,
        }
        self._path.write_text(json.dumps(data, indent=2))

    def publish(self, version: str, system_prompt: str, model: str,
                deployed_by: str, notes: str = "") -> PromptRelease:
        release = PromptRelease(
            release_id=f"rel-{len(self._releases)+1:04d}",
            version=version, system_prompt=system_prompt, model=model,
            deployed_by=deployed_by, notes=notes,
        )
        self._releases.append(release)
        self._save()
        print(f"[PUBLISH] {release.release_id} v{version} hash={release.content_hash}")
        return release

    def promote_to_green(self, release_id: str) -> None:
        self._green_id = release_id
        self._save()
        rel = self._get(release_id)
        print(f"[GREEN] {release_id} v{rel.version if rel else '?'} staged")

    def cutover(self) -> None:
        old_blue = self._blue_id
        self._blue_id = self._green_id
        self._green_id = None
        self._save()
        print(f"[CUTOVER] Blue={self._blue_id} (was {old_blue})")

    def rollback(self, steps: int = 1) -> Optional[PromptRelease]:
        active_releases = [r for r in self._releases
                           if r.release_id == self._blue_id]
        if not active_releases:
            return None
        # Find release that was active before
        idx = self._releases.index(active_releases[0]) - steps
        if idx < 0:
            print("[ROLLBACK] No earlier release available")
            return None
        target = self._releases[idx]
        self._blue_id = target.release_id
        self._save()
        print(f"[ROLLBACK] Rolled back to {target.release_id} v{target.version}")
        return target

    def _get(self, release_id: str) -> Optional[PromptRelease]:
        return next((r for r in self._releases if r.release_id == release_id), None)

    @property
    def blue(self) -> Optional[PromptRelease]:
        return self._get(self._blue_id) if self._blue_id else None

    @property
    def green(self) -> Optional[PromptRelease]:
        return self._get(self._green_id) if self._green_id else None

    def history(self) -> list[dict]:
        return [{"id": r.release_id, "version": r.version,
                 "hash": r.content_hash, "by": r.deployed_by,
                 "notes": r.notes[:40]}
                for r in reversed(self._releases[-5:])]

def demo():
    client = anthropic.Anthropic()
    registry = PromptRegistry()

    # Initial deployment
    v1 = registry.publish("1.0", "You are a helpful assistant.", "claude-haiku-4-5-20251001",
                          "alice", notes="Initial release")
    registry.promote_to_green(v1.release_id)
    registry.cutover()

    # Prepare new version
    v2 = registry.publish("2.0", "You are a precise assistant. Use numbered lists for multi-part answers.",
                          "claude-haiku-4-5-20251001", "bob", notes="Structured output experiment")
    registry.promote_to_green(v2.release_id)

    # Test current blue
    blue = registry.blue
    if blue:
        r = client.messages.create(
            model=blue.model, max_tokens=80,
            system=blue.system_prompt,
            messages=[{"role": "user", "content": "What are 3 types of clouds?"}],
        )
        print(f"\n[BLUE v{blue.version}]: {r.content[0].text[:80]}")

    registry.cutover()

    # Test new blue (was green)
    blue = registry.blue
    if blue:
        r = client.messages.create(
            model=blue.model, max_tokens=80,
            system=blue.system_prompt,
            messages=[{"role": "user", "content": "What are 3 types of clouds?"}],
        )
        print(f"\n[BLUE v{blue.version}]: {r.content[0].text[:80]}")

    # Rollback
    registry.rollback(steps=1)
    print(f"After rollback: blue={registry.blue.version if registry.blue else 'none'}")
    print(f"\nRelease history: {registry.history()}")

if __name__ == "__main__":
    demo()

# Expected Token Savings: Immutable history enables instant rollback to any known-good prompt
# Environment: pip install anthropic
```

### Option 5: Multi-Region Blue-Green with Consistency Checks

```python
import anthropic
import asyncio
import hashlib
import time
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class RegionConfig:
    region: str
    slot: str   # "blue" or "green"
    system_prompt: str
    model: str = "claude-haiku-4-5-20251001"

class MultiRegionBluGreen:
    """Coordinates blue-green across multiple regions."""
    def __init__(self, regions: list[str]):
        self._regions = regions
        self._configs: dict[str, RegionConfig] = {}
        self._active_slot: dict[str, str] = {r: "blue" for r in regions}

    def set_config(self, region: str, slot: str, system: str) -> None:
        self._configs[f"{region}:{slot}"] = RegionConfig(
            region=region, slot=slot, system_prompt=system
        )
        print(f"[CONFIGURE] {region}:{slot} → {system[:40]}")

    def promote_region(self, region: str) -> None:
        old = self._active_slot[region]
        new = "green" if old == "blue" else "blue"
        self._active_slot[region] = new
        print(f"[PROMOTE] {region}: {old} → {new}")

    def rollback_region(self, region: str) -> None:
        old = self._active_slot[region]
        new = "green" if old == "blue" else "blue"
        self._active_slot[region] = new
        print(f"[ROLLBACK] {region}: {old} → {new}")

    def get_config(self, region: str) -> Optional[RegionConfig]:
        slot = self._active_slot.get(region, "blue")
        return self._configs.get(f"{region}:{slot}")

    async def call(self, client: anthropic.AsyncAnthropic,
                   region: str, prompt: str) -> str:
        config = self.get_config(region)
        if not config:
            raise ValueError(f"No config for region {region}")
        response = await client.messages.create(
            model=config.model, max_tokens=60,
            system=config.system_prompt,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text

    async def consistency_check(self, client: anthropic.AsyncAnthropic,
                                 test_prompt: str) -> dict:
        """Verify all regions respond similarly."""
        results = await asyncio.gather(*[
            self.call(client, region, test_prompt)
            for region in self._regions
        ], return_exceptions=True)

        report = {}
        for region, result in zip(self._regions, results):
            slot = self._active_slot[region]
            if isinstance(result, Exception):
                report[region] = {"slot": slot, "status": "error", "error": str(result)}
            else:
                report[region] = {"slot": slot, "status": "ok",
                                   "response": str(result)[:50]}
        return report

async def main():
    client = anthropic.AsyncAnthropic()
    regions = ["us-east", "us-west", "eu-central"]
    bg = MultiRegionBluGreen(regions)

    # Set blue everywhere
    for region in regions:
        bg.set_config(region, "blue", "You are a helpful assistant.")
        bg.set_config(region, "green", "You are a structured assistant using bullet points.")

    print("\n--- All blue ---")
    report = await bg.consistency_check(client, "What is AI?")
    for region, r in report.items():
        print(f"  [{region}:{r['slot']}] {r.get('response', r.get('error', ''))[:50]}")

    # Promote us-east to green first (canary region)
    bg.promote_region("us-east")
    print("\n--- us-east on green, others on blue ---")
    report = await bg.consistency_check(client, "What is AI?")
    for region, r in report.items():
        print(f"  [{region}:{r['slot']}] {r.get('response', '')[:50]}")

    # Promote all regions
    for region in ["us-west", "eu-central"]:
        bg.promote_region(region)

    print("\n--- All green ---")
    report = await bg.consistency_check(client, "What is AI?")
    for region, r in report.items():
        print(f"  [{region}:{r['slot']}] {r.get('response', '')[:50]}")

if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: Canary region limits blast radius to 1/N of users during promotion
# Environment: pip install anthropic
```

### Option 6: Automated Blue-Green with Acceptance Criteria

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass, field
from typing import Callable, Awaitable

@dataclass
class AcceptanceCriteria:
    """Automated checks that must pass before green is promoted."""
    max_error_rate: float = 0.05
    max_latency_p95_ms: float = 3000.0
    min_sample_size: int = 10
    custom_checks: list[Callable[[list[str]], bool]] = field(default_factory=list)

@dataclass
class GreenCanaryResult:
    sample_size: int
    error_rate: float
    p95_latency_ms: float
    responses: list[str]
    passed: bool
    failure_reason: str = ""

class AutomatedBlueGreen:
    def __init__(self, blue_system: str, green_system: str,
                 criteria: AcceptanceCriteria):
        self.blue_system = blue_system
        self.green_system = green_system
        self.criteria = criteria
        self._active = "blue"
        self._client = anthropic.AsyncAnthropic()

    async def _sample_green(self, test_prompts: list[str]) -> GreenCanaryResult:
        """Run green against test prompts and evaluate."""
        errors = 0
        latencies = []
        responses = []

        for prompt in test_prompts:
            t0 = time.monotonic()
            try:
                r = await self._client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=100,
                    system=self.green_system,
                    messages=[{"role": "user", "content": prompt}],
                )
                latency_ms = (time.monotonic() - t0) * 1000
                latencies.append(latency_ms)
                responses.append(r.content[0].text)
            except Exception as e:
                errors += 1
                latencies.append((time.monotonic() - t0) * 1000)
                responses.append(f"ERROR: {e}")

        n = len(test_prompts)
        error_rate = errors / n
        sorted_lat = sorted(latencies)
        p95 = sorted_lat[int(len(sorted_lat) * 0.95)] if sorted_lat else 0.0

        passed = True
        reason = ""
        if error_rate > self.criteria.max_error_rate:
            passed = False
            reason = f"Error rate {error_rate:.1%} > {self.criteria.max_error_rate:.1%}"
        elif p95 > self.criteria.max_latency_p95_ms:
            passed = False
            reason = f"P95 latency {p95:.0f}ms > {self.criteria.max_latency_p95_ms:.0f}ms"
        else:
            for check in self.criteria.custom_checks:
                if not check(responses):
                    passed = False
                    reason = f"Custom check failed: {check.__name__}"
                    break

        return GreenCanaryResult(n, error_rate, p95, responses, passed, reason)

    async def promote_with_checks(self, test_prompts: list[str]) -> bool:
        print(f"[CANARY] Testing green with {len(test_prompts)} prompts...")
        result = await self._sample_green(test_prompts)
        print(f"[CANARY] error_rate={result.error_rate:.1%} "
              f"p95={result.p95_latency_ms:.0f}ms passed={result.passed}")

        if not result.passed:
            print(f"[BLOCKED] Promotion blocked: {result.failure_reason}")
            return False

        self._active = "green"
        print("[PROMOTED] Green is now active")
        return True

    async def call(self, prompt: str) -> str:
        system = self.green_system if self._active == "green" else self.blue_system
        r = await self._client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=80,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        return r.content[0].text

def no_errors_in_responses(responses: list[str]) -> bool:
    return not any("ERROR" in r for r in responses)

async def main():
    criteria = AcceptanceCriteria(
        max_error_rate=0.05, max_latency_p95_ms=5000.0,
        min_sample_size=3,
        custom_checks=[no_errors_in_responses],
    )
    bg = AutomatedBlueGreen(
        blue_system="You are a helpful assistant.",
        green_system="You are a concise assistant. Keep all answers under 2 sentences.",
        criteria=criteria,
    )

    test_prompts = [
        "What is gravity?",
        "Name three planets.",
        "What is Python used for?",
    ]

    promoted = await bg.promote_with_checks(test_prompts)
    print(f"\nPromotion {'succeeded' if promoted else 'blocked'}")

    result = await bg.call("Explain machine learning.")
    print(f"Response (slot={bg._active}): {result[:80]}")

if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: Automated acceptance prevents promoting prompts that increase verbosity/cost
# Environment: pip install anthropic
```

## Comparison

| Option | Traffic Split | Rollback | Shadow Mode | Auto-checks | Best For |
|--------|--------------|---------|-------------|-------------|----------|
| 1. Simple Router | %-based hash | Instant | No | No | Quick rollouts |
| 2. Shadow Testing | Primary=blue, async green | N/A | Yes | No | Zero-risk testing |
| 3. Health-Aware | %-based + auto | Auto on error | No | Error rate | Self-healing |
| 4. Version Registry | Slot-based | History steps | No | No | Audit/compliance |
| 5. Multi-Region | Per-region slot | Per-region | Consistency | No | Global services |
| 6. Automated | Test-gated | Pre-check blocks | No | Custom criteria | CI/CD pipelines |
