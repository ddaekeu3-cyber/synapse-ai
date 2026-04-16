---
title: "Agent Doesn't Implement Version Negotiation for Tool APIs"
description: "AI agents that hard-code tool API versions break silently when providers deprecate endpoints or change schemas. Learn six patterns for graceful API version negotiation that detect available versions, negotiate compatibility, and fall back gracefully."
date: 2026-04-16
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-version-negotiation-for-tool-apis
tags: [versioning, API, backward-compatibility, negotiation, deprecation, reliability]
symptoms:
  - "Agent breaks with HTTP 410 Gone after a tool provider deprecates v1 endpoint"
  - "New API version returns a different schema that the agent can't parse"
  - "Version mismatch between agent and tool goes undetected until runtime failure"
  - "Multiple agent instances running different API versions due to staggered deployments"
  - "No way to test compatibility with a new API version before switching traffic"
---

## The Problem

AI agents call tool APIs (search engines, databases, external services) that evolve independently. When a tool provider releases v2 and deprecates v1, agents that hard-code version strings break without warning. The fix — updating the version string — requires a deployment. More subtly, v2 might have a different response schema, requiring code changes beyond just the version string.

Version negotiation means the agent discovers which API versions are available, selects the highest mutually compatible version, adapts to the selected version's schema, and degrades gracefully when no compatible version exists.

```python
# ❌ Hard-coded version — breaks on deprecation
url = "https://api.example.com/v1/search"  # v1 deprecated → HTTP 410

# ✓ Version negotiation
negotiator = APIVersionNegotiator("https://api.example.com")
url, adapter = await negotiator.negotiate(supported=["v3", "v2", "v1"])
results = adapter.parse(await http.get(url + "/search"))
```

---

## Solution 1: Version Discovery via OPTIONS/Headers

Discover available API versions by querying a discovery endpoint or reading `API-Version` response headers, then select the highest mutually supported version.

```python
import asyncio
import time
from dataclasses import dataclass
from typing import Any
import aiohttp


@dataclass
class APIVersionInfo:
    version: str
    deprecated: bool
    sunset_date: str | None
    schema_url: str | None
    supported_features: list[str]


class APIVersionDiscovery:
    """
    Discovers available API versions from a service discovery endpoint
    or from standard headers (API-Version, Sunset, Deprecation).
    """

    DISCOVERY_PATHS = [
        "/.well-known/api-versions",
        "/api/versions",
        "/versions",
    ]

    def __init__(self, base_url: str, cache_ttl: float = 3600.0):
        self.base_url = base_url.rstrip("/")
        self.cache_ttl = cache_ttl
        self._cached: dict[str, Any] | None = None
        self._cached_at: float = 0.0

    async def discover(self) -> list[APIVersionInfo]:
        if self._cached and time.time() - self._cached_at < self.cache_ttl:
            return self._cached

        for path in self.DISCOVERY_PATHS:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        f"{self.base_url}{path}",
                        timeout=aiohttp.ClientTimeout(total=5),
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            versions = self._parse_discovery(data)
                            self._cached = versions
                            self._cached_at = time.time()
                            return versions
            except Exception:
                continue

        # Fallback: probe common version paths
        return await self._probe_versions()

    def _parse_discovery(self, data: dict | list) -> list[APIVersionInfo]:
        versions = []
        items = data if isinstance(data, list) else data.get("versions", [])
        for item in items:
            if isinstance(item, str):
                versions.append(APIVersionInfo(
                    version=item, deprecated=False,
                    sunset_date=None, schema_url=None, supported_features=[],
                ))
            elif isinstance(item, dict):
                versions.append(APIVersionInfo(
                    version=item.get("version", ""),
                    deprecated=item.get("deprecated", False),
                    sunset_date=item.get("sunset"),
                    schema_url=item.get("schema"),
                    supported_features=item.get("features", []),
                ))
        return [v for v in versions if v.version]

    async def _probe_versions(self) -> list[APIVersionInfo]:
        """Probe /v1, /v2, /v3 to see which exist."""
        found = []
        async with aiohttp.ClientSession() as session:
            for v in ["v4", "v3", "v2", "v1"]:
                try:
                    async with session.head(
                        f"{self.base_url}/{v}/",
                        timeout=aiohttp.ClientTimeout(total=3),
                        allow_redirects=True,
                    ) as resp:
                        if resp.status < 404:
                            deprecated = "Deprecation" in resp.headers or "Sunset" in resp.headers
                            sunset = resp.headers.get("Sunset")
                            found.append(APIVersionInfo(
                                version=v, deprecated=deprecated,
                                sunset_date=sunset, schema_url=None, supported_features=[],
                            ))
                except Exception:
                    pass
        return found

    def extract_from_response(self, headers: dict) -> dict:
        """Extract version info from API response headers."""
        return {
            "api_version": headers.get("API-Version") or headers.get("X-API-Version"),
            "deprecated": "Deprecation" in headers,
            "sunset": headers.get("Sunset"),
            "supported_versions": headers.get("X-Supported-Versions", "").split(","),
        }
```

---

## Solution 2: Semantic Version Negotiator

Given a list of agent-supported versions and provider-available versions, select the highest mutually compatible version using semantic versioning rules.

```python
from dataclasses import dataclass
from typing import Any
import re


@dataclass
class VersionRange:
    min_version: str
    max_version: str | None = None

    def includes(self, version: str) -> bool:
        return (self._cmp(version, self.min_version) >= 0 and
                (self.max_version is None or self._cmp(version, self.max_version) <= 0))

    @staticmethod
    def _cmp(a: str, b: str) -> int:
        """Compare two semver strings. Returns -1, 0, or 1."""
        def parse(v: str) -> tuple[int, ...]:
            v = v.lstrip("vV")
            parts = re.split(r'[.\-]', v)
            result = []
            for p in parts[:3]:
                try:
                    result.append(int(p))
                except ValueError:
                    result.append(0)
            while len(result) < 3:
                result.append(0)
            return tuple(result)
        pa, pb = parse(a), parse(b)
        if pa < pb:
            return -1
        elif pa > pb:
            return 1
        return 0


class SemanticVersionNegotiator:
    """
    Negotiates the highest mutually compatible API version.
    Supports exact versions, ranges, and minimum-version constraints.
    """

    def __init__(self, agent_supported: list[str]):
        """agent_supported: versions the agent can handle, e.g. ["v3", "v2", "v1"]"""
        self._supported = sorted(
            agent_supported,
            key=lambda v: [int(x) for x in re.findall(r'\d+', v)],
            reverse=True,
        )

    def negotiate(
        self,
        provider_available: list["APIVersionInfo"],
        prefer_latest: bool = True,
    ) -> tuple[str | None, str]:
        """
        Returns (selected_version, reason).
        selected_version is None if no compatible version found.
        """
        available_versions = {v.version for v in provider_available if not v.deprecated}
        deprecated_versions = {v.version for v in provider_available if v.deprecated}

        # Try non-deprecated versions first
        for version in self._supported:
            if version in available_versions:
                return version, f"selected_latest_compatible:{version}"

        # Fallback to deprecated versions (with warning)
        for version in self._supported:
            if version in deprecated_versions:
                info = next((v for v in provider_available if v.version == version), None)
                sunset_msg = f" (sunset: {info.sunset_date})" if info and info.sunset_date else ""
                print(f"[version] WARNING: Using deprecated API version {version}{sunset_msg}")
                return version, f"deprecated_fallback:{version}"

        return None, f"no_compatible_version (agent supports: {self._supported})"

    def log_sunset_warnings(self, versions: list["APIVersionInfo"]):
        """Log warnings for deprecated versions the agent is using."""
        for v in versions:
            if v.deprecated and v.version in self._supported:
                msg = f"[version] API version {v.version} is deprecated"
                if v.sunset_date:
                    msg += f" — sunset on {v.sunset_date}"
                print(msg)
```

---

## Solution 3: Response Schema Adapter

Different API versions return different response schemas. An adapter normalizes version-specific responses into a canonical internal format the agent always works with.

```python
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class SearchResult:
    """Canonical internal search result format."""
    title: str
    url: str
    snippet: str
    score: float = 0.0
    metadata: dict = field(default_factory=dict)


class SearchAPIAdapter:
    """
    Normalizes search API responses across versions into SearchResult objects.
    Adding a new version = adding one adapter function.
    """

    @staticmethod
    def _adapt_v1(raw: dict) -> list[SearchResult]:
        """v1 format: {results: [{title, link, description}]}"""
        return [
            SearchResult(
                title=r.get("title", ""),
                url=r.get("link", ""),
                snippet=r.get("description", ""),
            )
            for r in raw.get("results", [])
        ]

    @staticmethod
    def _adapt_v2(raw: dict) -> list[SearchResult]:
        """v2 format: {items: [{headline, href, summary, relevance}]}"""
        return [
            SearchResult(
                title=r.get("headline", ""),
                url=r.get("href", ""),
                snippet=r.get("summary", ""),
                score=r.get("relevance", 0.0),
            )
            for r in raw.get("items", [])
        ]

    @staticmethod
    def _adapt_v3(raw: dict) -> list[SearchResult]:
        """v3 format: {data: {hits: [{_source: {title, url, text}, _score}]}}"""
        hits = raw.get("data", {}).get("hits", [])
        return [
            SearchResult(
                title=h.get("_source", {}).get("title", ""),
                url=h.get("_source", {}).get("url", ""),
                snippet=h.get("_source", {}).get("text", ""),
                score=h.get("_score", 0.0),
                metadata=h.get("_source", {}),
            )
            for h in hits
        ]

    ADAPTERS: dict[str, Callable] = {
        "v1": _adapt_v1.__func__,
        "v2": _adapt_v2.__func__,
        "v3": _adapt_v3.__func__,
    }

    def adapt(self, version: str, raw_response: dict) -> list[SearchResult]:
        adapter = self.ADAPTERS.get(version)
        if not adapter:
            raise ValueError(f"No adapter for API version: {version}")
        return adapter(raw_response)

    def supports(self, version: str) -> bool:
        return version in self.ADAPTERS

    @property
    def supported_versions(self) -> list[str]:
        return list(self.ADAPTERS.keys())


class GenericSchemaAdapter:
    """
    Field-mapping adapter for cases where version differences are just field renames.
    More maintainable than writing a full adapter function per version.
    """

    VERSION_FIELD_MAPS: dict[str, dict[str, str]] = {
        "v1": {"title": "title", "url": "link", "snippet": "description"},
        "v2": {"title": "headline", "url": "href", "snippet": "summary"},
        "v3": {"title": "_source.title", "url": "_source.url", "snippet": "_source.text"},
    }

    def _get_nested(self, obj: dict, path: str) -> Any:
        parts = path.split(".")
        for part in parts:
            if isinstance(obj, dict):
                obj = obj.get(part, "")
            else:
                return ""
        return obj

    def normalize(self, version: str, item: dict) -> dict:
        field_map = self.VERSION_FIELD_MAPS.get(version, {})
        return {
            canonical: self._get_nested(item, source_path)
            for canonical, source_path in field_map.items()
        }
```

---

## Solution 4: Versioned Client with Automatic Retry on Version Error

When an API call fails with a version-related error (410 Gone, 400 Bad Request with version message), automatically retry with the next supported version.

```python
import asyncio
import aiohttp
import time
from dataclasses import dataclass


@dataclass
class VersionedRequest:
    method: str
    path: str
    data: dict | None = None
    attempted_versions: list[str] = None

    def __post_init__(self):
        if self.attempted_versions is None:
            self.attempted_versions = []


class VersionRetryClient:
    """
    HTTP client that retries with the next lower version on version errors.
    Maintains a per-endpoint version cache to avoid repeated negotiation.
    """

    VERSION_ERROR_STATUSES = {410, 400, 422}
    VERSION_ERROR_MESSAGES = [
        "version not supported", "api version", "unsupported version",
        "endpoint deprecated", "use version",
    ]

    def __init__(self, base_url: str, versions: list[str]):
        self.base_url = base_url.rstrip("/")
        self.versions = versions  # Ordered: preferred first
        self._version_cache: dict[str, str] = {}   # path → working version
        self._adapter = SearchAPIAdapter()

    def _is_version_error(self, status: int, body: str) -> bool:
        if status in self.VERSION_ERROR_STATUSES:
            body_lower = body.lower()
            return any(msg in body_lower for msg in self.VERSION_ERROR_MESSAGES)
        return status == 410  # Gone is always a version error

    async def request(self, req: VersionedRequest) -> tuple[dict, str]:
        """Returns (response_data, version_used)."""
        # Check if we have a cached working version for this path
        cached_version = self._version_cache.get(req.path)
        versions_to_try = (
            [cached_version] + [v for v in self.versions if v != cached_version]
            if cached_version else self.versions
        )

        last_error = None
        async with aiohttp.ClientSession() as session:
            for version in versions_to_try:
                if version in req.attempted_versions:
                    continue
                url = f"{self.base_url}/{version}{req.path}"
                req.attempted_versions.append(version)

                try:
                    kwargs = {"timeout": aiohttp.ClientTimeout(total=10)}
                    if req.method == "GET":
                        resp_ctx = session.get(url, **kwargs)
                    else:
                        resp_ctx = session.post(url, json=req.data, **kwargs)

                    async with resp_ctx as resp:
                        body = await resp.text()
                        if self._is_version_error(resp.status, body):
                            print(f"[version] {version} rejected for {req.path}: HTTP {resp.status}")
                            last_error = f"version_error:{resp.status}"
                            continue
                        if resp.status >= 400:
                            raise aiohttp.ClientError(f"HTTP {resp.status}: {body[:200]}")

                        # Success — cache this version for the path
                        import json
                        data = json.loads(body)
                        self._version_cache[req.path] = version
                        return data, version

                except aiohttp.ClientError as e:
                    if "version" in str(e).lower():
                        last_error = str(e)
                        continue
                    raise

        raise RuntimeError(
            f"All versions failed for {req.path}. "
            f"Tried: {req.attempted_versions}. Last error: {last_error}"
        )

    def invalidate_cache(self, path: str | None = None):
        if path:
            self._version_cache.pop(path, None)
        else:
            self._version_cache.clear()

    def version_cache_status(self) -> dict:
        return dict(self._version_cache)
```

---

## Solution 5: Contract Testing Before Version Switch

Before switching from a working version to a new one, run contract tests against the new version to verify compatibility without affecting production traffic.

```python
import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class ContractTest:
    test_name: str
    request_fn: Callable      # async fn(client, version) → response
    assertion_fn: Callable    # fn(response) → (passed, reason)


@dataclass
class ContractTestResult:
    test_name: str
    version: str
    passed: bool
    reason: str
    response_sample: Any = None
    latency_ms: float = 0.0


class APIContractTester:
    """
    Runs contract tests against a new API version before promoting it.
    Verifies that the new version satisfies all requirements.
    """

    def __init__(self, client: VersionRetryClient):
        self._client = client
        self._tests: list[ContractTest] = []

    def add_test(self, test: ContractTest):
        self._tests.append(test)

    def add_schema_test(self, path: str, required_fields: list[str]):
        """Convenience: add a test that checks response has required fields."""
        async def request_fn(client, version):
            data, _ = await client.request(VersionedRequest("GET", path))
            return data

        def assertion_fn(response):
            if not isinstance(response, dict):
                return False, f"Expected dict, got {type(response).__name__}"
            missing = [f for f in required_fields if f not in response]
            if missing:
                return False, f"Missing required fields: {missing}"
            return True, "schema_ok"

        self._tests.append(ContractTest(
            test_name=f"schema_test:{path}",
            request_fn=request_fn,
            assertion_fn=assertion_fn,
        ))

    async def test_version(self, version: str) -> list[ContractTestResult]:
        """Run all contract tests against a specific API version."""
        results = []
        for test in self._tests:
            start = time.monotonic()
            try:
                response = await test.request_fn(self._client, version)
                latency_ms = (time.monotonic() - start) * 1000
                passed, reason = test.assertion_fn(response)
                results.append(ContractTestResult(
                    test_name=test.test_name,
                    version=version,
                    passed=passed,
                    reason=reason,
                    response_sample=str(response)[:200],
                    latency_ms=latency_ms,
                ))
            except Exception as e:
                results.append(ContractTestResult(
                    test_name=test.test_name,
                    version=version,
                    passed=False,
                    reason=f"exception:{e}",
                    latency_ms=(time.monotonic() - start) * 1000,
                ))

        pass_count = sum(1 for r in results if r.passed)
        print(
            f"[contract] Version {version}: {pass_count}/{len(results)} tests passed"
        )
        return results

    def all_passed(self, results: list[ContractTestResult]) -> bool:
        return all(r.passed for r in results)

    async def safe_upgrade(
        self, current_version: str, candidate_version: str
    ) -> tuple[bool, str, list[ContractTestResult]]:
        """
        Test the candidate version. Return (should_upgrade, reason, test_results).
        """
        results = await self.test_version(candidate_version)
        if self.all_passed(results):
            return True, f"all_{len(results)}_contract_tests_passed", results
        failures = [r for r in results if not r.passed]
        return False, f"{len(failures)}_tests_failed", results
```

---

## Solution 6: Version Lifecycle Manager

Track API version deprecation timelines, alert before sunset dates, and orchestrate version upgrades across all agent instances.

```python
import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class VersionLifecycleStage(Enum):
    STABLE = "stable"
    DEPRECATED = "deprecated"
    SUNSET_IMMINENT = "sunset_imminent"  # < 30 days
    SUNSET = "sunset"


@dataclass
class VersionLifecycle:
    version: str
    stage: VersionLifecycleStage
    deprecated_since: str | None
    sunset_date: str | None
    successor_version: str | None
    days_until_sunset: int | None = None


class APIVersionLifecycleManager:
    """
    Tracks API version lifecycles and orchestrates upgrades.
    Alerts when versions approach sunset and blocks use of sunsetted versions.
    """

    def __init__(self, service_name: str):
        self.service_name = service_name
        self._lifecycles: dict[str, VersionLifecycle] = {}
        self._current_version: str | None = None
        self._upgrade_scheduled = False

    def register_lifecycle(self, lifecycle: VersionLifecycle):
        self._lifecycles[lifecycle.version] = lifecycle
        if lifecycle.days_until_sunset is not None and lifecycle.days_until_sunset < 30:
            lifecycle.stage = VersionLifecycleStage.SUNSET_IMMINENT

    def set_current_version(self, version: str):
        self._current_version = version

    def check_current_version(self) -> dict:
        if not self._current_version:
            return {"status": "unknown"}
        lifecycle = self._lifecycles.get(self._current_version)
        if not lifecycle:
            return {"status": "unregistered", "version": self._current_version}

        status = {
            "version": self._current_version,
            "stage": lifecycle.stage.value,
            "days_until_sunset": lifecycle.days_until_sunset,
            "successor": lifecycle.successor_version,
        }

        if lifecycle.stage == VersionLifecycleStage.SUNSET:
            status["action_required"] = "IMMEDIATE_UPGRADE_REQUIRED"
            print(
                f"[version_lifecycle] CRITICAL: {self.service_name} v{self._current_version} "
                f"has reached sunset. Migrate to {lifecycle.successor_version} immediately."
            )
        elif lifecycle.stage == VersionLifecycleStage.SUNSET_IMMINENT:
            status["action_required"] = f"upgrade_within_{lifecycle.days_until_sunset}_days"
            print(
                f"[version_lifecycle] WARNING: {self.service_name} v{self._current_version} "
                f"sunsets in {lifecycle.days_until_sunset} days. "
                f"Plan upgrade to {lifecycle.successor_version}."
            )
        elif lifecycle.stage == VersionLifecycleStage.DEPRECATED:
            status["action_required"] = "plan_upgrade"

        return status

    def recommended_version(self) -> str | None:
        """Return the newest non-deprecated version."""
        stable = [
            lc for lc in self._lifecycles.values()
            if lc.stage == VersionLifecycleStage.STABLE
        ]
        if not stable:
            return None
        return max(stable, key=lambda lc: lc.version).version

    def upgrade_path(self, from_version: str) -> list[str]:
        """
        Return the step-by-step upgrade path from current to latest.
        Some APIs require incremental upgrades (v1→v2→v3, not v1→v3).
        """
        path = []
        current = from_version
        for _ in range(10):  # Max 10 hops to prevent infinite loop
            lc = self._lifecycles.get(current)
            if not lc or not lc.successor_version:
                break
            path.append(lc.successor_version)
            current = lc.successor_version
        return path

    def prometheus_metrics(self) -> str:
        lines = []
        for version, lc in self._lifecycles.items():
            lbl = f'service="{self.service_name}",version="{version}"'
            stage_num = list(VersionLifecycleStage).index(lc.stage)
            lines.append(f'api_version_lifecycle_stage{{{lbl}}} {stage_num}')
            if lc.days_until_sunset is not None:
                lines.append(f'api_version_days_until_sunset{{{lbl}}} {lc.days_until_sunset}')
        return "\n".join(lines)
```

---

## Comparison

| Pattern | Detects Deprecation | Auto-Adapts Schema | Zero-Downtime Upgrade | Best For |
|---|---|---|---|---|
| Version discovery | Yes | No | No | Initial discovery of available versions |
| Semantic negotiator | Yes | No | No | Choosing the right version at startup |
| Response schema adapter | No | Yes | Yes | Handling different response shapes per version |
| Version retry client | Yes (via 410) | No | Yes (auto-retry) | Runtime version fallback without deployment |
| Contract tester | Yes (pre-upgrade) | No | Yes (test first) | Validating new version before switching |
| Lifecycle manager | Yes (sunset alerts) | No | Yes (orchestrated) | Long-term version management and migration planning |

**Recommendations:**
- Use **version discovery** (Solution 1) at startup to detect what versions the provider offers.
- Use **semantic negotiator** (Solution 2) to select the highest compatible version automatically.
- Implement **response schema adapters** (Solution 3) for every external tool API — they're the only way to handle v1→v2 schema changes without changing call-site code.
- Add **version retry client** (Solution 4) as a safety net so HTTP 410 errors cause automatic version downgrade rather than agent failure.
- Run **contract tests** (Solution 5) in CI/CD before merging any agent code that touches a versioned tool API.
- Monitor **lifecycle timelines** (Solution 6) and set alerts for sunset dates — 30 days is not enough to plan an upgrade.
