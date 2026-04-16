---
title: "Agent Doesn't Implement Dependency Version Pinning for Tool Integrations"
description: "Agents whose tool integrations depend on external API schemas, SDK versions, or protocol contracts without explicit version pinning break silently when upstream providers change their interfaces. A tool that calls a REST API without specifying an API version receives the latest version after a provider migration, causing field renames, removed endpoints, or changed authentication flows to surface as runtime errors. Implement version pinning that explicitly declares and enforces the expected interface version for every external tool dependency."
date: 2026-04-16
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-dependency-version-pinning-for-tool-integrations
tags: [version-pinning, api-versioning, tool-dependencies, interface-contract, breaking-changes, dependency-management]
symptoms:
  - "Tool breaks after provider silently upgrades their API to a new default version"
  - "Field names in tool responses change unexpectedly after a provider update"
  - "No record of which API version a tool was tested and validated against"
  - "Tool failures that trace back to upstream schema changes are impossible to diagnose quickly"
  - "Cannot roll back to a known-good API version when a new version introduces regressions"
---

## Why This Happens

External APIs evolve. A provider may rename a field, change a response envelope, deprecate an endpoint, or alter authentication requirements. Agents that call APIs without pinning a version receive whatever the provider serves as the current default — which may differ from what the agent was tested against. Version pinning requires including a version identifier in every API request (header, query parameter, or URL path segment) and maintaining a manifest of tested versions per tool. When a provider announces a deprecation, the agent can proactively test against the new version before the deadline rather than reacting to a production failure.

## Solution 1: Tool Dependency Manifest

```python
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional


class VersionStatus(str, Enum):
    ACTIVE = "active"
    DEPRECATED = "deprecated"
    SUNSET = "sunset"   # will be removed; migration required
    UNTESTED = "untested"


@dataclass
class APIVersionPin:
    tool_name: str
    provider: str
    api_version: str              # e.g., "2024-01-01", "v3", "3.1"
    version_header: Optional[str] = None   # header name to send the version in
    version_param: Optional[str] = None    # query param name
    version_path_prefix: Optional[str] = None  # URL prefix like "/v3"
    status: VersionStatus = VersionStatus.ACTIVE
    tested_at: Optional[float] = None
    sunset_date: Optional[str] = None
    migration_guide_url: Optional[str] = None
    notes: str = ""

    def is_deprecated(self) -> bool:
        return self.status in (VersionStatus.DEPRECATED, VersionStatus.SUNSET)


@dataclass
class ToolDependencyManifest:
    manifest_version: str = "1.0"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    pins: Dict[str, APIVersionPin] = field(default_factory=dict)

    def add(self, pin: APIVersionPin) -> None:
        self.pins[pin.tool_name] = pin
        self.updated_at = time.time()

    def get(self, tool_name: str) -> Optional[APIVersionPin]:
        return self.pins.get(tool_name)

    def deprecated_pins(self) -> List[APIVersionPin]:
        return [p for p in self.pins.values() if p.is_deprecated()]
```

## Solution 2: Version-Pinned HTTP Client

```python
from typing import Any, Dict, Optional


class VersionPinnedHTTPClient:
    """
    Injects API version identifiers into every request based on
    the registered pin for the tool making the call.
    """

    def __init__(self, manifest: ToolDependencyManifest):
        self._manifest = manifest

    def build_request_params(
        self,
        tool_name: str,
        base_url: str,
        headers: Optional[Dict[str, str]] = None,
        params: Optional[Dict[str, str]] = None,
    ) -> tuple:
        """
        Returns (url, headers, params) with version identifiers injected.
        """
        pin = self._manifest.get(tool_name)
        if pin is None:
            return base_url, headers or {}, params or {}

        url = base_url
        merged_headers = dict(headers or {})
        merged_params = dict(params or {})

        if pin.version_path_prefix:
            from urllib.parse import urlparse, urlunparse
            parsed = urlparse(base_url)
            new_path = pin.version_path_prefix.rstrip("/") + "/" + parsed.path.lstrip("/")
            url = urlunparse(parsed._replace(path=new_path))

        if pin.version_header and pin.version_header not in merged_headers:
            merged_headers[pin.version_header] = pin.api_version

        if pin.version_param and pin.version_param not in merged_params:
            merged_params[pin.version_param] = pin.api_version

        return url, merged_headers, merged_params
```

## Solution 3: Version Compatibility Checker

```python
import re
from typing import Any, Dict, List, Optional


class VersionCompatibilityChecker:
    """
    Validates API responses against expected schema signatures
    to detect field renames, type changes, or missing fields
    that indicate a version mismatch.
    """

    def __init__(self, manifest: ToolDependencyManifest):
        self._manifest = manifest
        self._schemas: Dict[str, dict] = {}

    def register_expected_schema(
        self,
        tool_name: str,
        required_fields: List[str],
        forbidden_fields: Optional[List[str]] = None,
    ) -> None:
        self._schemas[tool_name] = {
            "required": required_fields,
            "forbidden": forbidden_fields or [],
        }

    def check_response(
        self,
        tool_name: str,
        response: Any,
    ) -> List[str]:
        """Returns a list of compatibility warnings. Empty = compatible."""
        schema = self._schemas.get(tool_name)
        if not schema or not isinstance(response, dict):
            return []

        warnings = []
        for field in schema["required"]:
            if field not in response:
                warnings.append(f"missing expected field '{field}' — possible API version mismatch")

        for field in schema["forbidden"]:
            if field in response:
                warnings.append(f"found deprecated field '{field}' — API version may have regressed")

        return warnings
```

## Solution 4: Deprecation Monitor

```python
import time
from typing import List


class DeprecationMonitor:
    """
    Monitors the dependency manifest for upcoming deprecations and
    emits actionable warnings with time-to-sunset estimates.
    """

    def __init__(self, manifest: ToolDependencyManifest):
        self._manifest = manifest

    def check(self) -> List[dict]:
        issues = []
        for tool_name, pin in self._manifest.pins.items():
            if pin.status == VersionStatus.SUNSET:
                issues.append({
                    "severity": "critical",
                    "tool_name": tool_name,
                    "provider": pin.provider,
                    "api_version": pin.api_version,
                    "status": "sunset",
                    "sunset_date": pin.sunset_date,
                    "migration_guide": pin.migration_guide_url,
                    "action": "migrate immediately — version will be removed",
                })
            elif pin.status == VersionStatus.DEPRECATED:
                issues.append({
                    "severity": "high",
                    "tool_name": tool_name,
                    "provider": pin.provider,
                    "api_version": pin.api_version,
                    "status": "deprecated",
                    "sunset_date": pin.sunset_date,
                    "migration_guide": pin.migration_guide_url,
                    "action": "plan migration before sunset date",
                })
            elif pin.status == VersionStatus.UNTESTED:
                issues.append({
                    "severity": "medium",
                    "tool_name": tool_name,
                    "provider": pin.provider,
                    "api_version": pin.api_version,
                    "status": "untested",
                    "action": "run integration test to verify compatibility",
                })
        return sorted(issues, key=lambda i: {"critical": 0, "high": 1, "medium": 2}.get(i["severity"], 3))
```

## Solution 5: Version Drift Detector

```python
import time
from typing import List


class VersionDriftDetector:
    """
    Detects when a tool's actual API response version differs from
    the pinned version — indicating that the provider ignored or
    overrode the pinned version request.
    """

    def __init__(self, manifest: ToolDependencyManifest):
        self._manifest = manifest
        self._drift_events: list = []

    def check_response_version(
        self,
        tool_name: str,
        response_headers: dict,
        version_response_header: Optional[str] = None,
    ) -> Optional[dict]:
        pin = self._manifest.get(tool_name)
        if pin is None:
            return None

        header = version_response_header or pin.version_header
        if not header:
            return None

        actual_version = response_headers.get(header, "").strip()
        if not actual_version:
            return None

        if actual_version != pin.api_version:
            event = {
                "ts": time.time(),
                "tool_name": tool_name,
                "pinned_version": pin.api_version,
                "actual_version": actual_version,
            }
            self._drift_events.append(event)
            return event
        return None

    def drift_summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [e for e in self._drift_events if e["ts"] >= cutoff]
        return {
            "window_seconds": window_seconds,
            "drift_events": len(recent),
            "affected_tools": list({e["tool_name"] for e in recent}),
        }
```

## Solution 6: Dependency Version Dashboard

```python
import time


class DependencyVersionDashboard:
    """
    Combines manifest status, deprecation warnings, and drift detection.
    """

    def __init__(
        self,
        manifest: ToolDependencyManifest,
        deprecation_monitor: DeprecationMonitor,
        drift_detector: VersionDriftDetector,
    ):
        self._manifest = manifest
        self._monitor = deprecation_monitor
        self._drift = drift_detector

    def render(self, window_seconds: float = 3600.0) -> dict:
        pins = self._manifest.pins
        return {
            "generated_at": time.time(),
            "total_pinned_tools": len(pins),
            "active_pins": sum(1 for p in pins.values() if p.status == VersionStatus.ACTIVE),
            "deprecation_issues": self._monitor.check(),
            "version_drift": self._drift.drift_summary(window_seconds),
        }
```

## Comparison

| Approach | Version Injection | Schema Validation | Deprecation Alerts | Drift Detection | Dashboard |
|---|---|---|---|---|---|
| ToolDependencyManifest | Yes (registry) | No | No | No | No |
| VersionPinnedHTTPClient | Yes (header/param/path) | No | No | No | No |
| VersionCompatibilityChecker | No | Yes (field-level) | No | No | No |
| DeprecationMonitor | No | No | Yes (3 severities) | No | No |
| VersionDriftDetector | No | No | No | Yes | No |
| DependencyVersionDashboard | No | No | No | No | Yes |

**Best for production**: Store the `ToolDependencyManifest` as a committed file in the repository — version pins are part of the infrastructure contract and should be reviewed as code changes. Set up a weekly job that calls `DeprecationMonitor.check()` and sends results to the team's Slack channel — most API deprecations give 6-12 months notice, but teams miss the deadline because there's no automated reminder. Register expected schemas for all tools that parse structured API responses; run `VersionCompatibilityChecker.check_response()` in a canary environment against new API versions before pinning upgrades in production. Use `VersionDriftDetector` to detect providers that ignore version headers — a provider that consistently returns a different version than requested means the version header is non-functional and URL-path versioning is required.
