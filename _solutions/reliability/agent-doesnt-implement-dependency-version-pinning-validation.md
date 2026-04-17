---
title: "Agent Doesn't Implement Dependency Version Pinning Validation"
description: "Agents deployed without dependency version pinning validation silently run with unpinned or range-pinned packages that resolve to different versions across environments — causing subtle behavioral differences between staging and production, non-reproducible bugs, and supply chain risk from transitive dependency updates. Implement startup validation that checks installed package versions against a pinned manifest, blocks startup on mismatches, and reports drift."
date: 2026-04-16
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-dependency-version-pinning-validation
tags: [dependency-pinning, version-validation, reproducibility, supply-chain, startup-checks, environment-parity]
symptoms:
  - "Agent behaves differently in staging versus production despite identical code"
  - "A transitive dependency update broke the agent silently — no version change in requirements.txt"
  - "No startup check confirms installed packages match the pinned lockfile"
  - "pip install --upgrade run manually in production drifted versions from CI"
  - "Cannot determine which package version was running when a production incident occurred"
---

## Why This Happens

Python packages declared with range pins (`>=1.0,<2.0`) or no pins resolve to whatever version is current at install time. Two environments installed at different times will have different transitive dependency trees. Without a startup validation step that reads the installed package versions and compares them against a pinned lockfile, version drift is invisible. The agent runs with whichever versions happen to be installed, and subtle behavioral differences manifest as intermittent bugs or silent regressions. Startup validation converts this invisible risk into a loud, detectable failure that blocks the agent from serving traffic with an out-of-spec dependency tree.

## Solution 1: Pinned Dependency Manifest

```python
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional


@dataclass
class PinnedPackage:
    name: str
    pinned_version: str
    minimum_version: Optional[str] = None
    allow_patch_updates: bool = False   # if True, x.y.* is acceptable
    critical: bool = True              # if True, mismatch blocks startup


@dataclass
class PinnedDependencyManifest:
    schema_version: str = "1"
    packages: List[PinnedPackage] = field(default_factory=list)

    @classmethod
    def from_file(cls, path: str) -> "PinnedDependencyManifest":
        data = json.loads(Path(path).read_text())
        packages = [
            PinnedPackage(**pkg) for pkg in data.get("packages", [])
        ]
        return cls(
            schema_version=data.get("schema_version", "1"),
            packages=packages,
        )

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "packages": [
                {
                    "name": p.name,
                    "pinned_version": p.pinned_version,
                    "minimum_version": p.minimum_version,
                    "allow_patch_updates": p.allow_patch_updates,
                    "critical": p.critical,
                }
                for p in self.packages
            ],
        }
```

## Solution 2: Installed Package Scanner

```python
import importlib.metadata
from dataclasses import dataclass
from typing import Dict, Optional


@dataclass
class InstalledPackage:
    name: str
    version: str
    location: str = ""


class InstalledPackageScanner:
    """
    Reads installed package versions using importlib.metadata.
    Falls back to pkg_resources if metadata is unavailable.
    """

    def scan(self) -> Dict[str, InstalledPackage]:
        packages = {}
        try:
            for dist in importlib.metadata.distributions():
                name = dist.metadata.get("Name", "").lower()
                version = dist.metadata.get("Version", "unknown")
                if name:
                    packages[name] = InstalledPackage(
                        name=name,
                        version=version,
                    )
        except Exception:
            pass
        return packages

    def get_version(self, package_name: str) -> Optional[str]:
        try:
            return importlib.metadata.version(package_name)
        except importlib.metadata.PackageNotFoundError:
            return None
```

## Solution 3: Version Comparator

```python
from typing import Tuple


class VersionComparator:
    """
    Compares semantic version strings for pinning validation.
    Supports exact match, patch-level flexibility (x.y.*), and minimum version.
    """

    @staticmethod
    def parse(version: str) -> Tuple[int, ...]:
        try:
            parts = version.strip().split(".")
            return tuple(int(p) for p in parts if p.isdigit())
        except Exception:
            return (0,)

    def is_acceptable(
        self,
        installed: str,
        pinned: str,
        allow_patch_updates: bool = False,
        minimum_version: str = None,
    ) -> bool:
        if allow_patch_updates:
            # Accept x.y.* — installed major.minor must match pinned major.minor
            pinned_parts = self.parse(pinned)
            installed_parts = self.parse(installed)
            if len(pinned_parts) >= 2 and len(installed_parts) >= 2:
                if pinned_parts[:2] != installed_parts[:2]:
                    return False
                if minimum_version:
                    return installed_parts >= self.parse(minimum_version)
                return True
            return installed == pinned

        if minimum_version:
            min_parts = self.parse(minimum_version)
            installed_parts = self.parse(installed)
            return installed == pinned or installed_parts >= min_parts

        return installed == pinned
```

## Solution 4: Dependency Version Validator

```python
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import List


class ValidationStatus(str, Enum):
    OK = "ok"
    MISMATCH = "mismatch"
    MISSING = "missing"
    WARNING = "warning"


@dataclass
class PackageValidationResult:
    package_name: str
    pinned_version: str
    installed_version: str
    status: ValidationStatus
    critical: bool
    message: str = ""


class DependencyVersionValidator:
    """
    Validates installed package versions against a pinned manifest.
    Returns per-package results and a pass/fail overall decision.
    """

    def __init__(
        self,
        manifest: PinnedDependencyManifest,
        scanner: InstalledPackageScanner,
        comparator: VersionComparator,
    ):
        self._manifest = manifest
        self._scanner = scanner
        self._comparator = comparator

    def validate(self) -> dict:
        installed = self._scanner.scan()
        results = []
        has_critical_failure = False

        for pinned_pkg in self._manifest.packages:
            name_lower = pinned_pkg.name.lower()
            inst_pkg = installed.get(name_lower)

            if inst_pkg is None:
                inst_version = self._scanner.get_version(pinned_pkg.name)
                if inst_version is None:
                    status = ValidationStatus.MISSING
                    result = PackageValidationResult(
                        package_name=pinned_pkg.name,
                        pinned_version=pinned_pkg.pinned_version,
                        installed_version="NOT INSTALLED",
                        status=status,
                        critical=pinned_pkg.critical,
                        message=f"Package {pinned_pkg.name} is not installed",
                    )
                    if pinned_pkg.critical:
                        has_critical_failure = True
                    results.append(result)
                    continue
                installed_version = inst_version
            else:
                installed_version = inst_pkg.version

            acceptable = self._comparator.is_acceptable(
                installed=installed_version,
                pinned=pinned_pkg.pinned_version,
                allow_patch_updates=pinned_pkg.allow_patch_updates,
                minimum_version=pinned_pkg.minimum_version,
            )

            if acceptable:
                status = ValidationStatus.OK
                message = ""
            else:
                status = ValidationStatus.MISMATCH if pinned_pkg.critical else ValidationStatus.WARNING
                message = (
                    f"Expected {pinned_pkg.pinned_version}, found {installed_version}"
                )
                if pinned_pkg.critical:
                    has_critical_failure = True

            results.append(PackageValidationResult(
                package_name=pinned_pkg.name,
                pinned_version=pinned_pkg.pinned_version,
                installed_version=installed_version,
                status=status,
                critical=pinned_pkg.critical,
                message=message,
            ))

        return {
            "validated_at": time.time(),
            "passed": not has_critical_failure,
            "total_checked": len(results),
            "mismatches": sum(1 for r in results if r.status == ValidationStatus.MISMATCH),
            "missing": sum(1 for r in results if r.status == ValidationStatus.MISSING),
            "warnings": sum(1 for r in results if r.status == ValidationStatus.WARNING),
            "results": results,
        }
```

## Solution 5: Startup Version Gate

```python
import sys


class StartupVersionGate:
    """
    Runs dependency validation at startup and blocks agent initialization
    if critical package mismatches are detected.
    Prints a human-readable report to stderr before exiting.
    """

    def __init__(
        self,
        validator: DependencyVersionValidator,
        exit_on_failure: bool = True,
    ):
        self._validator = validator
        self._exit_on_failure = exit_on_failure

    def check(self) -> dict:
        report = self._validator.validate()

        if not report["passed"]:
            self._print_failure_report(report)
            if self._exit_on_failure:
                sys.exit(1)
        elif report["warnings"] > 0:
            self._print_warning_report(report)

        return report

    @staticmethod
    def _print_failure_report(report: dict) -> None:
        print("STARTUP BLOCKED: Dependency version mismatch detected", file=sys.stderr)
        for r in report["results"]:
            if r.status in (ValidationStatus.MISMATCH, ValidationStatus.MISSING):
                print(
                    f"  [CRITICAL] {r.package_name}: {r.message}",
                    file=sys.stderr,
                )

    @staticmethod
    def _print_warning_report(report: dict) -> None:
        for r in report["results"]:
            if r.status == ValidationStatus.WARNING:
                print(
                    f"  [WARNING] {r.package_name}: {r.message}",
                    file=sys.stderr,
                )
```

## Solution 6: Dependency Drift Reporter

```python
import importlib.metadata
import time
from typing import Dict, List


class DependencyDriftReporter:
    """
    Produces a complete snapshot of all installed packages for
    lockfile generation and drift comparison across deployments.
    """

    def __init__(self, scanner: InstalledPackageScanner):
        self._scanner = scanner

    def snapshot(self) -> dict:
        installed = self._scanner.scan()
        return {
            "captured_at": time.time(),
            "package_count": len(installed),
            "packages": {
                name: pkg.version
                for name, pkg in sorted(installed.items())
            },
        }

    def diff(self, baseline: dict, current: dict) -> dict:
        baseline_pkgs = baseline.get("packages", {})
        current_pkgs = current.get("packages", {})
        added = {k: v for k, v in current_pkgs.items() if k not in baseline_pkgs}
        removed = {k: v for k, v in baseline_pkgs.items() if k not in current_pkgs}
        changed = {
            k: {"from": baseline_pkgs[k], "to": current_pkgs[k]}
            for k in baseline_pkgs
            if k in current_pkgs and baseline_pkgs[k] != current_pkgs[k]
        }
        return {
            "added": added,
            "removed": removed,
            "changed": changed,
            "drift_detected": bool(added or removed or changed),
        }
```

## Comparison

| Approach | Manifest Loading | Version Scanning | Mismatch Detection | Startup Block | Drift Reporting |
|---|---|---|---|---|---|
| PinnedDependencyManifest | Yes (JSON) | No | No | No | No |
| InstalledPackageScanner | No | Yes (importlib) | No | No | No |
| VersionComparator | No | No | Yes (exact + patch) | No | No |
| DependencyVersionValidator | Via manifest | Via scanner | Yes (per-package) | No | No |
| StartupVersionGate | Via validator | Via validator | Via validator | Yes (sys.exit) | No |
| DependencyDriftReporter | No | Via scanner | No | No | Yes (diff) |

**Best for production**: Pin all direct dependencies to exact versions in the manifest (`allow_patch_updates=False` for security-sensitive packages like cryptography and requests). Run `StartupVersionGate.check()` as the very first operation in the agent entrypoint — before any model client initialization — so mismatches fail fast without partial initialization side effects. Mark packages like `anthropic`, `openai`, `langchain`, and `pydantic` as `critical=True`: these have frequent breaking changes. Run `DependencyDriftReporter.diff()` between the CI lockfile snapshot and the production snapshot as part of every deployment verification step to catch cases where pip installed different transitive versions in prod.
