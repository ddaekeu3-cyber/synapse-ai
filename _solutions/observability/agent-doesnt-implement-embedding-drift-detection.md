---
title: "Agent Doesn't Implement Embedding Drift Detection"
description: "Agents that rely on vector search degrade silently when the embedding model changes or input distribution shifts, causing retrieval quality to collapse without any visible error. Implement embedding drift detection to catch distribution shifts before they impact users."
date: 2026-04-16
difficulty: advanced
category: observability
slug: agent-doesnt-implement-embedding-drift-detection
tags: [embedding-drift, vector-search, retrieval-quality, observability, distribution-shift, monitoring]
symptoms:
  - "Retrieval relevance degrades after embedding model upgrade with no alerts"
  - "Cosine similarity scores trend lower over weeks without explanation"
  - "RAG answers become less grounded after corpus re-indexing"
  - "New query patterns return stale or irrelevant chunks silently"
  - "No baseline to compare current embedding distribution against"
---

## Why This Happens

Embedding models produce vectors in a high-dimensional space that encodes semantic meaning. When the model is updated, fine-tuned, or replaced, existing indexed vectors are no longer in the same space as new query vectors. Similarly, if user query patterns shift significantly, the query vector distribution may diverge from the indexed corpus distribution. Neither case throws an exception — similarity scores just quietly decline. Drift detection measures this divergence continuously and alerts before retrieval quality collapses.

## Solution 1: Centroid Drift Monitor with Cosine Distance

```python
import asyncio
import numpy as np
from dataclasses import dataclass, field
from typing import List, Optional, Deque
from collections import deque

@dataclass
class EmbeddingSnapshot:
    timestamp: float
    centroid: np.ndarray
    sample_size: int
    avg_norm: float
    std_norm: float

class CentroidDriftMonitor:
    """
    Tracks the centroid of recent query embeddings.
    Computes cosine distance between rolling centroid and a fixed baseline.
    Alert when drift exceeds threshold.
    """

    def __init__(self, window_size: int = 1000, drift_threshold: float = 0.05):
        self._window: Deque[np.ndarray] = deque(maxlen=window_size)
        self._baseline: Optional[EmbeddingSnapshot] = None
        self._drift_threshold = drift_threshold

    def add(self, embedding: np.ndarray) -> None:
        self._window.append(embedding / (np.linalg.norm(embedding) + 1e-9))

    def freeze_baseline(self) -> None:
        if len(self._window) < 10:
            raise ValueError("Need at least 10 samples to establish baseline")
        vecs = np.stack(list(self._window))
        centroid = vecs.mean(axis=0)
        norms = np.linalg.norm(vecs, axis=1)
        self._baseline = EmbeddingSnapshot(
            timestamp=__import__("time").time(),
            centroid=centroid,
            sample_size=len(self._window),
            avg_norm=float(norms.mean()),
            std_norm=float(norms.std()),
        )

    def current_centroid(self) -> Optional[np.ndarray]:
        if not self._window:
            return None
        return np.stack(list(self._window)).mean(axis=0)

    def cosine_drift(self) -> Optional[float]:
        if self._baseline is None:
            return None
        current = self.current_centroid()
        if current is None:
            return None
        b = self._baseline.centroid
        cos_sim = float(np.dot(current, b) / (np.linalg.norm(current) * np.linalg.norm(b) + 1e-9))
        return 1.0 - cos_sim  # drift = 1 - similarity

    def is_drifted(self) -> bool:
        drift = self.cosine_drift()
        return drift is not None and drift > self._drift_threshold

    def report(self) -> dict:
        drift = self.cosine_drift()
        return {
            "sample_count": len(self._window),
            "has_baseline": self._baseline is not None,
            "cosine_drift": drift,
            "drifted": drift is not None and drift > self._drift_threshold,
            "threshold": self._drift_threshold,
        }
```

## Solution 2: Distribution Distance with Maximum Mean Discrepancy (MMD)

```python
import numpy as np
from typing import List

class MMDDriftDetector:
    """
    Maximum Mean Discrepancy test between baseline and current embedding distributions.
    Model-free, works on any dimensionality.
    Uses RBF kernel: k(x,y) = exp(-||x-y||^2 / (2*sigma^2))
    """

    def __init__(self, sigma: float = 1.0, sample_size: int = 500):
        self._sigma = sigma
        self._sample_size = sample_size
        self._baseline_samples: Optional[np.ndarray] = None

    def _rbf_kernel(self, X: np.ndarray, Y: np.ndarray) -> np.ndarray:
        dists = np.sum((X[:, None] - Y[None, :]) ** 2, axis=-1)
        return np.exp(-dists / (2 * self._sigma ** 2))

    def _mmd(self, X: np.ndarray, Y: np.ndarray) -> float:
        kxx = self._rbf_kernel(X, X).mean()
        kyy = self._rbf_kernel(Y, Y).mean()
        kxy = self._rbf_kernel(X, Y).mean()
        return float(kxx - 2 * kxy + kyy)

    def set_baseline(self, embeddings: List[np.ndarray]) -> None:
        arr = np.stack(embeddings)
        if len(arr) > self._sample_size:
            idx = np.random.choice(len(arr), self._sample_size, replace=False)
            arr = arr[idx]
        # Project to lower dim for tractability (random projection)
        self._baseline_samples = self._project(arr)

    def _project(self, arr: np.ndarray, target_dim: int = 64) -> np.ndarray:
        if arr.shape[1] <= target_dim:
            return arr
        rng = np.random.RandomState(42)
        proj = rng.randn(arr.shape[1], target_dim) / np.sqrt(target_dim)
        return arr @ proj

    def mmd_score(self, current_embeddings: List[np.ndarray]) -> Optional[float]:
        if self._baseline_samples is None:
            return None
        arr = np.stack(current_embeddings)
        if len(arr) > self._sample_size:
            idx = np.random.choice(len(arr), self._sample_size, replace=False)
            arr = arr[idx]
        current_proj = self._project(arr)
        return self._mmd(self._baseline_samples, current_proj)

    def is_drifted(self, current_embeddings: List[np.ndarray], threshold: float = 0.01) -> bool:
        score = self.mmd_score(current_embeddings)
        return score is not None and score > threshold
```

## Solution 3: Retrieval Quality Probe — Anchor Query Regression

```python
import asyncio
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional

@dataclass
class AnchorQuery:
    query: str
    expected_doc_ids: List[str]   # top-k expected results
    min_recall_at_k: float = 0.8  # at least 80% of expected docs in top-k

@dataclass
class ProbeResult:
    query: str
    recall_at_k: float
    avg_similarity: float
    timestamp: float
    passed: bool

class AnchorQueryProbe:
    """
    Maintains a set of "anchor" queries with known-good expected results.
    Periodically re-runs them against the live vector store and measures
    recall@k. A drop signals embedding or index drift.
    """

    def __init__(
        self,
        anchors: List[AnchorQuery],
        embed_fn: Callable[[str], asyncio.Future],
        search_fn: Callable[[List[float], int], asyncio.Future],
        k: int = 10,
    ):
        self._anchors = anchors
        self._embed = embed_fn
        self._search = search_fn
        self._k = k
        self._history: List[ProbeResult] = []

    async def probe_one(self, anchor: AnchorQuery) -> ProbeResult:
        embedding = await self._embed(anchor.query)
        results = await self._search(embedding, self._k)
        returned_ids = {r["doc_id"] for r in results[:self._k]}
        expected_ids = set(anchor.expected_doc_ids)
        recall = len(returned_ids & expected_ids) / len(expected_ids) if expected_ids else 1.0
        avg_sim = float(sum(r.get("similarity", 0) for r in results[:self._k]) / max(len(results), 1))
        result = ProbeResult(
            query=anchor.query,
            recall_at_k=recall,
            avg_similarity=avg_sim,
            timestamp=time.time(),
            passed=recall >= anchor.min_recall_at_k,
        )
        self._history.append(result)
        return result

    async def run_all(self) -> List[ProbeResult]:
        results = await asyncio.gather(*[self.probe_one(a) for a in self._anchors])
        return list(results)

    def overall_recall(self) -> float:
        recent = self._history[-len(self._anchors):]
        if not recent:
            return 1.0
        return sum(r.recall_at_k for r in recent) / len(recent)

    def any_failed(self) -> bool:
        recent = self._history[-len(self._anchors):]
        return any(not r.passed for r in recent)

    async def monitor_loop(self, interval_seconds: float = 300.0) -> None:
        while True:
            await asyncio.sleep(interval_seconds)
            results = await self.run_all()
            failed = [r for r in results if not r.passed]
            if failed:
                print(
                    f"[anchor_probe] DRIFT DETECTED: {len(failed)}/{len(results)} probes failed. "
                    f"overall_recall={self.overall_recall():.2%}"
                )
            else:
                print(f"[anchor_probe] OK: overall_recall={self.overall_recall():.2%}")
```

## Solution 4: Similarity Score Distribution Tracker

```python
import numpy as np
from collections import deque
from dataclasses import dataclass
from typing import Deque, Optional

@dataclass
class SimilarityStats:
    p50: float
    p95: float
    p99: float
    mean: float
    std: float
    sample_count: int

class SimilarityDistributionTracker:
    """
    Tracks the distribution of top-1 cosine similarity scores from live queries.
    A downward shift in p50/p95 indicates retrieval quality degradation.
    Uses Kolmogorov-Smirnov test to detect significant distribution change.
    """

    def __init__(self, window_size: int = 2000):
        self._baseline: Optional[np.ndarray] = None
        self._current: Deque[float] = deque(maxlen=window_size)

    def add_score(self, top1_similarity: float) -> None:
        self._current.append(top1_similarity)

    def freeze_baseline(self) -> None:
        if len(self._current) < 100:
            raise ValueError("Need at least 100 samples for baseline")
        self._baseline = np.array(list(self._current))

    def current_stats(self) -> Optional[SimilarityStats]:
        if len(self._current) < 10:
            return None
        arr = np.array(list(self._current))
        return SimilarityStats(
            p50=float(np.percentile(arr, 50)),
            p95=float(np.percentile(arr, 95)),
            p99=float(np.percentile(arr, 99)),
            mean=float(arr.mean()),
            std=float(arr.std()),
            sample_count=len(arr),
        )

    def ks_statistic(self) -> Optional[float]:
        """Kolmogorov-Smirnov test statistic between baseline and current."""
        if self._baseline is None or len(self._current) < 50:
            return None
        from scipy.stats import ks_2samp
        stat, _pvalue = ks_2samp(self._baseline, list(self._current))
        return float(stat)

    def p50_drop(self) -> Optional[float]:
        """Absolute drop in median similarity vs baseline."""
        if self._baseline is None:
            return None
        baseline_p50 = float(np.percentile(self._baseline, 50))
        stats = self.current_stats()
        if stats is None:
            return None
        return baseline_p50 - stats.p50  # positive = degradation

    def is_drifted(self, ks_threshold: float = 0.1, p50_drop_threshold: float = 0.05) -> bool:
        ks = self.ks_statistic()
        drop = self.p50_drop()
        if ks is not None and ks > ks_threshold:
            return True
        if drop is not None and drop > p50_drop_threshold:
            return True
        return False
```

## Solution 5: Embedding Model Version Change Detector

```python
import hashlib
import json
from dataclasses import dataclass
from typing import Optional

@dataclass
class EmbeddingModelFingerprint:
    model_id: str
    dim: int
    probe_vector_hash: str   # hash of a fixed probe text's embedding
    recorded_at: float

class EmbeddingModelChangeDetector:
    """
    Detects when the embedding model has been swapped by comparing
    a fixed probe text's embedding against a stored fingerprint.
    Even a minor model update changes the probe embedding measurably.
    """

    PROBE_TEXT = "the quick brown fox jumps over the lazy dog"

    def __init__(self, fingerprint_store, embed_fn):
        self._store = fingerprint_store
        self._embed = embed_fn

    async def record_fingerprint(self, model_id: str) -> EmbeddingModelFingerprint:
        import time
        vec = await self._embed(self.PROBE_TEXT)
        vec_hash = hashlib.sha256(
            json.dumps([round(float(x), 6) for x in vec]).encode()
        ).hexdigest()
        fp = EmbeddingModelFingerprint(
            model_id=model_id,
            dim=len(vec),
            probe_vector_hash=vec_hash,
            recorded_at=time.time(),
        )
        await self._store.save(fp)
        return fp

    async def check_for_change(self, current_model_id: str) -> bool:
        """Returns True if a model change is detected."""
        stored = await self._store.load()
        if stored is None:
            return False  # No baseline — record one
        vec = await self._embed(self.PROBE_TEXT)
        current_hash = hashlib.sha256(
            json.dumps([round(float(x), 6) for x in vec]).encode()
        ).hexdigest()
        changed = (
            stored.probe_vector_hash != current_hash
            or stored.model_id != current_model_id
            or stored.dim != len(vec)
        )
        if changed:
            print(
                f"[embedding_model_detector] MODEL CHANGE DETECTED: "
                f"stored={stored.model_id} (dim={stored.dim}) "
                f"current={current_model_id} (dim={len(vec)}). "
                f"Re-index required!"
            )
        return changed
```

## Solution 6: Unified Embedding Drift Pipeline

```python
import asyncio
import time
from typing import List

class EmbeddingDriftPipeline:
    """
    Combines centroid drift, similarity distribution tracking,
    anchor query probing, and model change detection into a single
    background monitoring pipeline.
    """

    def __init__(
        self,
        centroid_monitor: CentroidDriftMonitor,
        similarity_tracker: SimilarityDistributionTracker,
        anchor_probe: AnchorQueryProbe,
        model_detector: EmbeddingModelChangeDetector,
        alert_fn,
        probe_interval_seconds: float = 300.0,
        check_interval_seconds: float = 60.0,
    ):
        self._centroid = centroid_monitor
        self._similarity = similarity_tracker
        self._probe = anchor_probe
        self._model = model_detector
        self._alert = alert_fn
        self._probe_interval = probe_interval_seconds
        self._check_interval = check_interval_seconds

    def record_query(self, embedding: "np.ndarray", top1_similarity: float) -> None:
        """Call this on every live query to feed the monitors."""
        self._centroid.add(embedding)
        self._similarity.add_score(top1_similarity)

    async def start(self, current_model_id: str) -> None:
        await asyncio.gather(
            self._check_loop(current_model_id),
            self._probe_loop(),
        )

    async def _check_loop(self, model_id: str) -> None:
        while True:
            await asyncio.sleep(self._check_interval)
            alerts = []

            if self._centroid.is_drifted():
                r = self._centroid.report()
                alerts.append(f"centroid_drift={r['cosine_drift']:.4f} (threshold={r['threshold']})")

            if self._similarity.is_drifted():
                drop = self._similarity.p50_drop()
                ks = self._similarity.ks_statistic()
                alerts.append(f"similarity_p50_drop={drop:.4f} ks_stat={ks:.4f}")

            if await self._model.check_for_change(model_id):
                alerts.append("embedding_model_changed — full re-index required")

            if alerts:
                await self._alert({
                    "severity": "warning",
                    "type": "embedding_drift",
                    "signals": alerts,
                    "timestamp": time.time(),
                })

    async def _probe_loop(self) -> None:
        while True:
            await asyncio.sleep(self._probe_interval)
            results = await self._probe.run_all()
            failed = [r for r in results if not r.passed]
            if failed:
                await self._alert({
                    "severity": "critical",
                    "type": "anchor_probe_failure",
                    "failed_queries": [r.query for r in failed],
                    "overall_recall": self._probe.overall_recall(),
                    "timestamp": time.time(),
                })

    def status(self) -> dict:
        return {
            "centroid": self._centroid.report(),
            "similarity_stats": (
                vars(self._similarity.current_stats())
                if self._similarity.current_stats() else None
            ),
            "anchor_recall": self._probe.overall_recall(),
            "ks_statistic": self._similarity.ks_statistic(),
            "p50_drop": self._similarity.p50_drop(),
        }
```

## Comparison

| Approach | Detects Model Swap | Detects Query Shift | Statistical Rigor | Overhead |
|---|---|---|---|---|
| CentroidDriftMonitor | Yes (centroid moves) | Yes | Low (cosine distance) | Very low |
| MMDDriftDetector | Yes | Yes | High (distribution test) | Medium (kernel computation) |
| AnchorQueryProbe | Yes (recall drops) | Partial | High (ground truth) | Low (periodic probes) |
| SimilarityDistributionTracker | Partial | Yes (score distribution) | Medium (KS test) | Very low |
| EmbeddingModelChangeDetector | Yes (exact fingerprint) | No | Exact | Very low |
| EmbeddingDriftPipeline | Yes (all signals) | Yes | Full composite | Low (background loops) |

**Best choice for production**: Run `EmbeddingDriftPipeline` combining all monitors. Use `EmbeddingModelChangeDetector` for instant model-swap detection, `AnchorQueryProbe` as the ground-truth gate, and `SimilarityDistributionTracker` (KS test) for continuous distribution health. Alert on any of the three signals failing.
