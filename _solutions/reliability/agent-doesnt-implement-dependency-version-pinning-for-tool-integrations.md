---
title: "Agent Doesn't Implement Dependency Version Pinning for Tool Integrations"
description: "Agents that declare tool integration dependencies with unpinned or loosely-bounded version constraints break silently when upstream packages release breaking changes: an SDK method is renamed, a response schema field is dropped, or a new required parameter is added. Implement dependency version pinning with runtime compatibility checks, version drift detection, and automated breakage detection before deployment."
date: 2026-04-16
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-dependency-version-pinning-for-tool-integrations
tags: [version-pinning, dependency-management, tool-compatibility, sdk-drift, breaking-changes, dependency-audit]
symptoms:
  - "Tool integration breaks after an automatic dependency upgrade in CI"
  - "Agent works in development but fails in production with a different package version"
  - "requirements.txt uses >= constraints that allow major version upgrades"
  - "No runtime check that the installed package version matches the tested version"
  - "Breaking SDK change is discovered only when the tool is called in production"
---

## Why This Happens

Python packaging conventions encourage `>=` version constraints for compatibility, but for agent tool integrations these constraints are too permissive. An SDK that changes its response schema or renames a method between minor versions will silently break the agent's tool adapter. Version pinning requires specifying exact versions (`==`) or tight upper bounds (`>=x.y,<x.(y+1)`) for every direct tool dependency, validating installed versions at startup, and running compatibility smoke tests in CI before merging dependency updates.

## Solution 1: Tool Dependency Specification

```python
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class ToolDependencySpec:
    package_name: str
    exact_version: Optional[str] = None       # e.g. "1.23.4" — strictest
    min_version: Optional[str] = None         # e.g. "1.23.0"
    max_version_exclusive: Optional[str] = None  # e.g. "1.24.0" (< this)
    tool_name: str = ""                       # which agent tool uses this dep
    critical: bool = True                     # block startup if incompatible

    def constraint_string(self) -> str:
        if self.exact_version:
            return f"{self.package_name}=={self.exact_version}"
        parts = []
        if self.min_version:
            parts.append(f">={self.min_version}")
        if self.max_version_exclusive:
            parts.append(f"<{self.max_version_exclusive}")
        return f"{self.package_name}{','.join(parts)}"
```

## Solution 2: Version Compatibility Checker

```python
import importlib.metadata
from dataclasses import dataclass
from typing import List, Optional, Tuple


def _parse_version(v: str) -> Tuple[int, ...]:
    try:
        return tuple(int(x) for x in v.split(".")[:3])
    except ValueError:
        return (0, 0, 0)


@dataclass
class CompatibilityCheckResult:
    package_name: str
    installed_version: Optional[str]
    required_constraint: str
    compatible: bool
    reason: str


class VersionCompatibilityChecker:
    """
    Checks that installed package versions satisfy the constraints
    defined in ToolDependencySpec. Runs at agent startup.
    """

    def check(self, spec: ToolDependencySpec) -> CompatibilityCheckResult:
        try:
            installed = importlib.metadata.version(spec.package_name)
        except importlib.metadata.PackageNotFoundError:
            return CompatibilityCheckResult(
                package_name=spec.package_name,
                installed_version=None,
                required_constraint=spec.constraint_string(),
                compatible=False,
                reason=f"Package '{spec.package_name}' is not installed",
            )

        installed_v = _parse_version(installed)

        if spec.exact_version:
            required_v = _parse_version(spec.exact_version)
            compatible = installed_v == required_v
            reason = (
                "exact version match"
                if compatible
                else f"expected {spec.exact_version}, got {installed}"
            )
        else:
            compatible = True
            reason = "within range"
            if spec.min_version:
                min_v = _parse_version(spec.min_version)
                if installed_v < min_v:
                    compatible = False
                    reason = f"{installed} < minimum {spec.min_version}"
            if compatible and spec.max_version_exclusive:
                max_v = _parse_version(spec.max_version_exclusive)
                if installed_v >= max_v:
                    compatible = False
                    reason = f"{installed} >= exclusive max {spec.max_version_exclusive}"

        return CompatibilityCheckResult(
            package_name=spec.package_name,
            installed_version=installed,
            required_constraint=spec.constraint_string(),
            compatible=compatible,
            reason=reason,
        )

    def check_all(
        self, specs: List[ToolDependencySpec]
    ) -> List[CompatibilityCheckResult]:
        return [self.check(spec) for spec in specs]
```

## Solution 3: Startup Dependency Gate

```python
from typing import List


class StartupDependencyGate:
    """
    Runs all dependency compatibility checks at agent startup.
    Raises RuntimeError if any critical dependency is incompatible,
    emitting a clear message about which package and what version is needed.
    """

    def __init__(self, checker: VersionCompatibilityChecker):
        self._checker = checker

    def validate(self, specs: List[ToolDependencySpec]) -> dict:
        results = self._checker.check_all(specs)
        failures = [r for r in results if not r.compatible and
                    next((s for s in specs if s.package_name == r.package_name
                          and s.critical), None)]
        warnings = [r for r in results if not r.compatible and
                    not next((s for s in specs if s.package_name == r.package_name
                               and s.critical), None)]

        if failures:
            lines = [
                f"  - {r.package_name} (installed: {r.installed_version}, "
                f"required: {r.required_constraint}): {r.reason}"
                for r in failures
            ]
            raise RuntimeError(
                "Agent startup blocked — incompatible tool dependencies:\n"
                + "\n".join(lines)
            )

        return {
            "checked": len(results),
            "compatible": sum(1 for r in results if r.compatible),
            "warnings": [
                {"package": r.package_name, "reason": r.reason}
                for r in warnings
            ],
        }
```

## Solution 4: Version Drift Detector

```python
import json
import time
from pathlib import Path
from typing import Dict, List, Optional


class VersionDriftDetector:
    """
    Compares currently installed versions against a saved baseline
    (e.g., the versions used in the last passing test run).
    Detects drift that could indicate an unauthorized upgrade.
    """

    def __init__(self, baseline_path: str = "/tmp/agent_dep_baseline.json"):
        self._path = Path(baseline_path)

    def save_baseline(self, specs: List[ToolDependencySpec]) -> None:
        import importlib.metadata
        baseline: Dict[str, str] = {}
        for spec in specs:
            try:
                baseline[spec.package_name] = importlib.metadata.version(spec.package_name)
            except importlib.metadata.PackageNotFoundError:
                baseline[spec.package_name] = "not_installed"
        self._path.write_text(json.dumps({
            "saved_at": time.time(),
            "versions": baseline,
        }, indent=2))

    def detect_drift(self, specs: List[ToolDependencySpec]) -> List[dict]:
        if not self._path.exists():
            return []
        import importlib.metadata
        baseline_data = json.loads(self._path.read_text())
        baseline = baseline_data.get("versions", {})
        drifted = []
        for spec in specs:
            baseline_v = baseline.get(spec.package_name)
            if baseline_v is None:
                continue
            try:
                current = importlib.metadata.version(spec.package_name)
            except importlib.metadata.PackageNotFoundError:
                current = "not_installed"
            if current != baseline_v:
                drifted.append({
                    "package": spec.package_name,
                    "baseline_version": baseline_v,
                    "current_version": current,
                    "tool": spec.tool_name,
                })
        return drifted
```

## Solution 5: Dependency Smoke Tester

```python
import time
from typing import Any, Callable, Dict, List, Optional


class DependencySmokeTester:
    """
    Runs lightweight smoke tests against each tool integration
    after a dependency update to detect breakage before deployment.
    Smoke tests are callables that return True on success.
    """

    def __init__(self):
        self._tests: Dict[str, Callable[[], bool]] = {}

    def register(self, package_name: str, test_fn: Callable[[], bool]) -> None:
        self._tests[package_name] = test_fn

    def run_all(self) -> List[dict]:
        results = []
        for pkg, fn in self._tests.items():
            start = time.time()
            try:
                passed = fn()
                error = None
            except Exception as exc:
                passed = False
                error = str(exc)
            results.append({
                "package": pkg,
                "passed": passed,
                "error": error,
                "duration_ms": round((time.time() - start) * 1000, 2),
            })
        return results

    def all_pass(self) -> bool:
        return all(r["passed"] for r in self.run_all())
```

## Solution 6: Dependency Audit Reporter

```python
import time
from typing import List


class DependencyAuditReporter:
    """
    Combines compatibility check results, drift detection, and
    smoke test outcomes into a single dependency health report.
    """

    def __init__(
        self,
        checker: VersionCompatibilityChecker,
        drift_detector: VersionDriftDetector,
        smoke_tester: DependencySmokeTester,
        specs: List[ToolDependencySpec],
    ):
        self._checker = checker
        self._drift = drift_detector
        self._smoke = smoke_tester
        self._specs = specs

    def report(self) -> dict:
        compat = self._checker.check_all(self._specs)
        drift = self._drift.detect_drift(self._specs)
        smoke = self._smoke.run_all()

        return {
            "generated_at": time.time(),
            "compatibility": {
                "total": len(compat),
                "compatible": sum(1 for r in compat if r.compatible),
                "incompatible": [
                    {"package": r.package_name, "reason": r.reason}
                    for r in compat if not r.compatible
                ],
            },
            "drift": {
                "drifted_packages": len(drift),
                "details": drift,
            },
            "smoke_tests": {
                "total": len(smoke),
                "passed": sum(1 for r in smoke if r["passed"]),
                "failed": [r for r in smoke if not r["passed"]],
            },
        }
```

## Comparison

| Approach | Version Check | Startup Gate | Drift Detection | Smoke Tests | Audit Report |
|---|---|---|---|---|---|
| VersionCompatibilityChecker | Yes (exact+range) | No | No | No | No |
| StartupDependencyGate | Via checker | Yes (blocks startup) | No | No | No |
| VersionDriftDetector | No | No | Yes (baseline diff) | No | No |
| DependencySmokeTester | No | No | No | Yes (callable) | No |
| DependencyAuditReporter | Via checker | No | Via detector | Via tester | Yes |

**Best for production**: Pin all direct tool integration dependencies to exact versions (`==`) in `requirements.txt` and use a separate `requirements-loose.txt` with `>=` constraints for transitive dependency resolution. Run `StartupDependencyGate.validate()` before the first request is handled — this catches version mismatches between the development environment and the deployed container before any user traffic is affected. Save a `VersionDriftDetector` baseline after every successful test run in CI; a drift detection alert in production means a dependency was upgraded outside the normal release process and the smoke tests should be run immediately to verify no breakage occurred.
