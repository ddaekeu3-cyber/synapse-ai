---
title: "Agent Doesn't Implement Knowledge Base Staleness Tracking"
description: "Agents that retrieve from a knowledge base without tracking document freshness serve outdated information confidently: a policy document updated three months ago, an API reference from last year, or product pricing that changed last week. Implement knowledge base staleness tracking that monitors document age, detects when retrieved content is likely outdated, and surfaces freshness signals alongside retrieved results."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-knowledge-base-staleness-tracking
tags: [knowledge-base, staleness-tracking, document-freshness, retrieval-quality, rag-observability, content-age]
symptoms:
  - "Agent cites a document last updated 18 months ago as current information"
  - "No freshness metadata attached to retrieved documents"
  - "Users receive confidently stated outdated facts because the knowledge base has not been refreshed"
  - "No alert when the average age of retrieved documents exceeds a configured threshold"
  - "Knowledge base ingestion pipeline runs but no one tracks whether documents are being updated"
---

## Why This Happens

RAG pipelines retrieve documents based on semantic similarity, not recency. A highly relevant but outdated document ranks above a moderately relevant but current one if its embedding distance is lower. Without freshness metadata attached to each document and surfaced alongside retrieved results, the agent has no signal that the content it is citing is stale. Staleness tracking requires storing an ingestion timestamp and an optional source-last-modified timestamp for each document, measuring the age at retrieval time, and emitting a warning when age exceeds a configured threshold for the document's domain.

## Solution 1: Document Freshness Record

```python
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class FreshnessStatus(str, Enum):
    FRESH = "fresh"
    AGING = "aging"
    STALE = "stale"
    UNKNOWN = "unknown"


@dataclass
class DocumentFreshnessRecord:
    doc_id: str
    source_url: str = ""
    ingested_at: float = field(default_factory=time.time)
    source_last_modified: Optional[float] = None   # from HTTP Last-Modified or source API
    domain_tag: str = ""                            # e.g. "pricing", "policy", "api_docs"
    expected_update_frequency_days: Optional[float] = None

    def age_days(self) -> float:
        ref_time = self.source_last_modified or self.ingested_at
        return (time.time() - ref_time) / 86400.0

    def freshness_status(self, stale_days: float = 90.0, aging_days: float = 30.0) -> FreshnessStatus:
        age = self.age_days()
        if self.source_last_modified is None and self.ingested_at == 0:
            return FreshnessStatus.UNKNOWN
        if age > stale_days:
            return FreshnessStatus.STALE
        if age > aging_days:
            return FreshnessStatus.AGING
        return FreshnessStatus.FRESH
```

## Solution 2: Freshness Threshold Registry

```python
from typing import Dict, Tuple


DEFAULT_THRESHOLDS: Dict[str, Tuple[float, float]] = {
    "pricing": (1.0, 7.0),          # stale after 1 day, aging after 7 hours
    "policy": (90.0, 30.0),         # stale after 90 days, aging after 30
    "api_docs": (30.0, 14.0),       # stale after 30 days, aging after 14
    "news": (0.25, 1.0),            # stale after 6 hours, aging after 1 day
    "product_catalog": (7.0, 3.0),  # stale after 7 days, aging after 3
    "documentation": (180.0, 60.0), # stale after 180 days, aging after 60
    "static": (3650.0, 365.0),      # effectively never stale
}


class FreshnessThresholdRegistry:
    """
    Returns (stale_days, aging_days) thresholds per domain tag.
    Falls back to conservative defaults for unknown domains.
    """

    DEFAULT_STALE = 90.0
    DEFAULT_AGING = 30.0

    def __init__(self, custom: Dict[str, Tuple[float, float]] = None):
        self._thresholds = {**DEFAULT_THRESHOLDS, **(custom or {})}

    def get(self, domain_tag: str) -> Tuple[float, float]:
        return self._thresholds.get(
            domain_tag, (self.DEFAULT_STALE, self.DEFAULT_AGING)
        )

    def register(self, domain_tag: str, stale_days: float, aging_days: float) -> None:
        self._thresholds[domain_tag] = (stale_days, aging_days)
```

## Solution 3: Retrieval Freshness Annotator

```python
import time
from typing import Any, Dict, List, Optional


class RetrievalFreshnessAnnotator:
    """
    Attaches freshness metadata to retrieved documents at query time.
    Returns the annotated documents with freshness status and age.
    """

    def __init__(
        self,
        threshold_registry: FreshnessThresholdRegistry,
        freshness_records: Dict[str, DocumentFreshnessRecord],
    ):
        self._thresholds = threshold_registry
        self._records = freshness_records

    def annotate(
        self,
        retrieved_docs: List[Dict[str, Any]],
    ) -> List[dict]:
        annotated = []
        for doc in retrieved_docs:
            doc_id = doc.get("id", doc.get("doc_id", ""))
            record = self._records.get(doc_id)

            if record is None:
                annotated.append({
                    **doc,
                    "freshness_status": FreshnessStatus.UNKNOWN.value,
                    "age_days": None,
                    "freshness_warning": "No freshness record available for this document.",
                })
                continue

            stale_days, aging_days = self._thresholds.get(record.domain_tag)
            status = record.freshness_status(stale_days, aging_days)
            age = round(record.age_days(), 1)

            warning = None
            if status == FreshnessStatus.STALE:
                warning = f"This document is {age:.0f} days old and may be outdated."
            elif status == FreshnessStatus.AGING:
                warning = f"This document is {age:.0f} days old — verify before relying on it."

            annotated.append({
                **doc,
                "freshness_status": status.value,
                "age_days": age,
                "domain_tag": record.domain_tag,
                "freshness_warning": warning,
            })

        return annotated

    def has_stale_content(self, annotated_docs: List[dict]) -> bool:
        return any(
            d.get("freshness_status") == FreshnessStatus.STALE.value
            for d in annotated_docs
        )
```

## Solution 4: Knowledge Base Staleness Monitor

```python
import time
from collections import defaultdict
from threading import Lock
from typing import Dict, List, Optional


class KnowledgeBaseStalenessMonitor:
    """
    Scans all registered freshness records and identifies
    documents that are stale or at risk of becoming stale.
    Supports alerting when domain-level staleness exceeds a threshold.
    """

    def __init__(
        self,
        records: Dict[str, DocumentFreshnessRecord],
        threshold_registry: FreshnessThresholdRegistry,
    ):
        self._records = records
        self._thresholds = threshold_registry

    def scan(self) -> dict:
        by_status: Dict[str, List[str]] = defaultdict(list)
        by_domain: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))

        for doc_id, record in self._records.items():
            stale_days, aging_days = self._thresholds.get(record.domain_tag)
            status = record.freshness_status(stale_days, aging_days)
            by_status[status.value].append(doc_id)
            by_domain[record.domain_tag][status.value] += 1

        return {
            "total_documents": len(self._records),
            "by_status": {k: len(v) for k, v in by_status.items()},
            "stale_doc_ids": by_status.get(FreshnessStatus.STALE.value, [])[:20],
            "by_domain": {
                domain: dict(counts)
                for domain, counts in by_domain.items()
            },
        }

    def stale_rate(self, domain_tag: str = "") -> float:
        if domain_tag:
            relevant = [
                r for r in self._records.values()
                if r.domain_tag == domain_tag
            ]
        else:
            relevant = list(self._records.values())

        if not relevant:
            return 0.0

        stale = sum(
            1 for r in relevant
            if r.freshness_status(*self._thresholds.get(r.domain_tag)) == FreshnessStatus.STALE
        )
        return round(stale / len(relevant), 4)
```

## Solution 5: Staleness Alert Manager

```python
import time
from typing import Callable, Dict, List, Optional


class StalenessAlertManager:
    """
    Fires alerts when domain-level stale rate exceeds configured thresholds
    or when high-priority documents have not been updated.
    """

    def __init__(
        self,
        monitor: KnowledgeBaseStalenessMonitor,
        alert_fn: Optional[Callable[[dict], None]] = None,
        stale_rate_threshold: float = 0.20,
    ):
        self._monitor = monitor
        self._alert_fn = alert_fn
        self._threshold = stale_rate_threshold
        self._alert_log: List[dict] = []

    def check_and_alert(self, domain_tags: List[str] = None) -> List[dict]:
        scan = self._monitor.scan()
        fired = []

        domains = domain_tags or list(scan["by_domain"].keys())
        for domain in domains:
            rate = self._monitor.stale_rate(domain)
            if rate >= self._threshold:
                alert = {
                    "domain": domain,
                    "stale_rate": rate,
                    "threshold": self._threshold,
                    "fired_at": time.time(),
                }
                self._alert_log.append(alert)
                if self._alert_fn:
                    self._alert_fn(alert)
                fired.append(alert)

        return fired

    def recent_alerts(self, limit: int = 20) -> List[dict]:
        return self._alert_log[-limit:]
```

## Solution 6: Knowledge Base Freshness Dashboard

```python
import time


class KnowledgeBaseFreshnessDashboard:
    """
    Combines staleness scan, domain-level breakdown, and
    alert history into a single freshness health report.
    """

    def __init__(
        self,
        monitor: KnowledgeBaseStalenessMonitor,
        alert_manager: StalenessAlertManager,
    ):
        self._monitor = monitor
        self._alerts = alert_manager

    def render(self) -> dict:
        scan = self._monitor.scan()
        return {
            "generated_at": time.time(),
            "overall_staleness_rate": round(
                scan["by_status"].get("stale", 0) / max(scan["total_documents"], 1), 4
            ),
            "scan": scan,
            "recent_alerts": self._alerts.recent_alerts(limit=5),
        }
```

## Comparison

| Approach | Age Measurement | Domain Thresholds | Retrieval Annotation | Staleness Scan | Alerting |
|---|---|---|---|---|---|
| DocumentFreshnessRecord | Yes (age_days) | No | No | No | No |
| FreshnessThresholdRegistry | No | Yes (per domain) | No | No | No |
| RetrievalFreshnessAnnotator | Via records | Via registry | Yes | No | No |
| KnowledgeBaseStalenessMonitor | Via records | Via registry | No | Yes | No |
| StalenessAlertManager | No | No | No | Via monitor | Yes |
| KnowledgeBaseFreshnessDashboard | No | No | No | Via monitor | Via manager |

**Best for production**: Store `source_last_modified` from HTTP `Last-Modified` headers or source API timestamps at ingestion time — this is more accurate than the ingestion timestamp, which reflects when the pipeline ran, not when the content was authored. Attach `freshness_warning` to every retrieved document in the context prompt so the LLM can caveat its response when citing aging or stale content. Set domain thresholds aggressively for high-stakes domains: `pricing` should be stale after 1 day, not 90. Run `StalenessAlertManager.check_and_alert()` after every retrieval batch — a retrieval that surfaces 3+ stale documents for a pricing query should trigger an immediate re-ingestion, not wait for the next scheduled pipeline run.
