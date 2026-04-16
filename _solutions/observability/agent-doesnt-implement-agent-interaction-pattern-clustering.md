---
title: "Agent Doesn't Implement Agent Interaction Pattern Clustering"
description: "Agents that log individual conversations without grouping them by behavioral pattern cannot answer fleet-level questions: which conversation flows are most common, which tool sequences correlate with failures, and which user request types drive the most cost. Implement interaction pattern clustering to extract feature vectors from conversations, group similar sessions with k-means or DBSCAN, and surface dominant patterns with representative examples."
date: 2026-04-16
difficulty: advanced
category: observability
slug: agent-doesnt-implement-agent-interaction-pattern-clustering
tags: [pattern-clustering, behavioral-analysis, conversation-analytics, unsupervised-learning, fleet-observability, usage-patterns]
symptoms:
  - "No answer to 'what are the top 10 ways users interact with this agent?'"
  - "Failure investigation starts from scratch each time — no pattern library to check first"
  - "Cost optimization impossible because tool-call sequences are never aggregated"
  - "Prompt engineering is guess-work because common input patterns are unknown"
  - "SLO violations repeat because root-cause patterns are not catalogued"
---

## Why This Happens

Agent observability tools focus on individual traces — a single conversation's latency, its tool calls, its token count. Aggregation across conversations is usually limited to simple averages. But the most actionable insights come from grouping: which conversation shapes appear repeatedly, which tool-call sequences are almost always followed by errors, which request phrasings cost 10× more than others. Clustering converts a stream of individual session records into a pattern library with named archetypes, frequency counts, and representative examples.

## Solution 1: Interaction Feature Extractor

```python
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class SessionRecord:
    session_id: str
    user_turns: List[str]
    assistant_turns: List[str]
    tool_call_sequence: List[str]       # e.g. ["search", "search", "summarize"]
    total_tokens: int
    total_latency_ms: float
    error_occurred: bool
    termination_reason: str             # "completed" | "timeout" | "error" | "user_abort"
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class InteractionFeatureVector:
    session_id: str
    # Structural features
    num_turns: int
    num_tool_calls: int
    unique_tool_count: int
    tool_sequence_hash: str
    # Cost features
    total_tokens: int
    tokens_per_turn: float
    # Outcome features
    error_flag: int                     # 0 or 1
    termination_code: int               # encoded termination reason
    # Content features (bag-of-words sketch)
    first_user_word_count: int
    question_ratio: float               # fraction of user turns ending with "?"
    raw: Dict[str, float] = field(default_factory=dict)   # for ML use


TERMINATION_CODES = {
    "completed": 0,
    "timeout": 1,
    "error": 2,
    "user_abort": 3,
}


class InteractionFeatureExtractor:
    """
    Converts a SessionRecord into a fixed-length feature vector
    suitable for distance-based clustering algorithms.
    """

    def extract(self, session: SessionRecord) -> InteractionFeatureVector:
        num_turns = len(session.user_turns)
        num_tool_calls = len(session.tool_call_sequence)
        unique_tools = len(set(session.tool_call_sequence))
        tool_hash = self._sequence_hash(session.tool_call_sequence)
        tokens_per_turn = session.total_tokens / max(num_turns, 1)
        question_ratio = self._question_ratio(session.user_turns)
        first_word_count = len(session.user_turns[0].split()) if session.user_turns else 0
        term_code = TERMINATION_CODES.get(session.termination_reason, -1)

        raw = {
            "num_turns": float(num_turns),
            "num_tool_calls": float(num_tool_calls),
            "unique_tool_count": float(unique_tools),
            "total_tokens": float(session.total_tokens),
            "tokens_per_turn": tokens_per_turn,
            "error_flag": float(session.error_occurred),
            "termination_code": float(term_code),
            "first_user_word_count": float(first_word_count),
            "question_ratio": question_ratio,
        }

        return InteractionFeatureVector(
            session_id=session.session_id,
            num_turns=num_turns,
            num_tool_calls=num_tool_calls,
            unique_tool_count=unique_tools,
            tool_sequence_hash=tool_hash,
            total_tokens=session.total_tokens,
            tokens_per_turn=tokens_per_turn,
            error_flag=int(session.error_occurred),
            termination_code=term_code,
            first_user_word_count=first_word_count,
            question_ratio=question_ratio,
            raw=raw,
        )

    @staticmethod
    def _sequence_hash(seq: List[str]) -> str:
        import hashlib
        return hashlib.md5(":".join(seq).encode()).hexdigest()[:8]

    @staticmethod
    def _question_ratio(turns: List[str]) -> float:
        if not turns:
            return 0.0
        questions = sum(1 for t in turns if t.strip().endswith("?"))
        return round(questions / len(turns), 3)
```

## Solution 2: Feature Normalizer

```python
import math
from typing import Dict, List


class FeatureNormalizer:
    """
    Z-score normalizes feature vectors so high-magnitude features
    (token counts) don't dominate distance calculations over
    low-magnitude ones (question ratio, error flag).
    Fit on a reference batch; transform incrementally thereafter.
    """

    def __init__(self):
        self._means: Dict[str, float] = {}
        self._stds: Dict[str, float] = {}
        self._fitted = False

    def fit(self, vectors: List[InteractionFeatureVector]) -> None:
        if not vectors:
            return
        keys = list(vectors[0].raw.keys())
        for key in keys:
            values = [v.raw[key] for v in vectors]
            mean = sum(values) / len(values)
            variance = sum((x - mean) ** 2 for x in values) / max(len(values) - 1, 1)
            self._means[key] = mean
            self._stds[key] = math.sqrt(variance) or 1.0
        self._fitted = True

    def transform(
        self, vector: InteractionFeatureVector
    ) -> List[float]:
        if not self._fitted:
            return list(vector.raw.values())
        return [
            (vector.raw.get(k, 0.0) - self._means[k]) / self._stds[k]
            for k in self._means
        ]

    def fit_transform(
        self, vectors: List[InteractionFeatureVector]
    ) -> List[List[float]]:
        self.fit(vectors)
        return [self.transform(v) for v in vectors]
```

## Solution 3: K-Means Pattern Clusterer

```python
import math
import random
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


@dataclass
class PatternCluster:
    cluster_id: int
    centroid: List[float]
    member_session_ids: List[str] = field(default_factory=list)
    label: str = ""                  # human-readable name assigned later
    error_rate: float = 0.0
    avg_tokens: float = 0.0
    avg_turns: float = 0.0
    dominant_tool_sequence: str = ""


def _euclidean(a: List[float], b: List[float]) -> float:
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


class KMeansPatternClusterer:
    """
    Groups interaction feature vectors into k clusters using k-means.
    Supports warm-start from existing centroids for incremental updates.
    """

    def __init__(
        self,
        k: int = 8,
        max_iterations: int = 100,
        tolerance: float = 1e-4,
        random_seed: int = 42,
    ):
        self._k = k
        self._max_iter = max_iterations
        self._tol = tolerance
        random.seed(random_seed)

    def fit(
        self,
        vectors: List[List[float]],
        session_ids: List[str],
        feature_vectors: List[InteractionFeatureVector],
    ) -> List[PatternCluster]:
        if len(vectors) < self._k:
            self._k = max(len(vectors), 1)

        # k-means++ initialisation
        centroids = [random.choice(vectors)]
        while len(centroids) < self._k:
            dists = [min(_euclidean(v, c) for c in centroids) for v in vectors]
            total = sum(dists)
            r = random.uniform(0, total)
            cumulative = 0.0
            for v, d in zip(vectors, dists):
                cumulative += d
                if cumulative >= r:
                    centroids.append(v)
                    break

        assignments = [0] * len(vectors)
        for iteration in range(self._max_iter):
            # Assign
            new_assignments = [
                min(range(len(centroids)), key=lambda j: _euclidean(v, centroids[j]))
                for v in vectors
            ]
            if new_assignments == assignments and iteration > 0:
                break
            assignments = new_assignments

            # Update centroids
            new_centroids = []
            dim = len(vectors[0])
            for j in range(len(centroids)):
                members = [vectors[i] for i, a in enumerate(assignments) if a == j]
                if members:
                    new_centroids.append(
                        [sum(m[d] for m in members) / len(members) for d in range(dim)]
                    )
                else:
                    new_centroids.append(centroids[j])

            # Convergence check
            shift = max(_euclidean(old, new) for old, new in zip(centroids, new_centroids))
            centroids = new_centroids
            if shift < self._tol:
                break

        # Build cluster objects
        clusters = [
            PatternCluster(cluster_id=j, centroid=centroids[j])
            for j in range(len(centroids))
        ]
        for i, j in enumerate(assignments):
            clusters[j].member_session_ids.append(session_ids[i])

        # Enrich with stats
        fv_by_id = {v.session_id: v for v in feature_vectors}
        for cluster in clusters:
            members = [fv_by_id[sid] for sid in cluster.member_session_ids if sid in fv_by_id]
            if members:
                cluster.error_rate = round(sum(m.error_flag for m in members) / len(members), 3)
                cluster.avg_tokens = round(sum(m.total_tokens for m in members) / len(members), 1)
                cluster.avg_turns = round(sum(m.num_turns for m in members) / len(members), 1)

        return clusters
```

## Solution 4: Pattern Labeler

```python
from typing import List


class PatternLabeler:
    """
    Assigns a human-readable label to each cluster based on its statistics.
    Labels describe the dominant behavior archetype for dashboards.
    """

    def label(self, cluster: PatternCluster) -> str:
        parts = []

        if cluster.avg_turns <= 2:
            parts.append("single-shot")
        elif cluster.avg_turns <= 5:
            parts.append("short-session")
        else:
            parts.append("long-session")

        if cluster.avg_tokens > 8000:
            parts.append("high-cost")
        elif cluster.avg_tokens < 1000:
            parts.append("low-cost")

        if cluster.error_rate > 0.3:
            parts.append("error-prone")
        elif cluster.error_rate < 0.02:
            parts.append("reliable")

        if cluster.dominant_tool_sequence:
            seq = cluster.dominant_tool_sequence[:30]
            parts.append(f"[{seq}]")

        return " / ".join(parts) if parts else f"cluster-{cluster.cluster_id}"

    def label_all(self, clusters: List[PatternCluster]) -> List[PatternCluster]:
        for cluster in clusters:
            cluster.label = self.label(cluster)
        return clusters
```

## Solution 5: Pattern Registry

```python
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class PatternSnapshot:
    snapshot_id: str
    created_at: float
    clusters: List[PatternCluster]
    total_sessions: int
    k: int


class PatternRegistry:
    """
    Stores pattern snapshots over time.
    Supports querying: which cluster does a new session belong to?
    Detects emerging clusters by comparing snapshots.
    """

    def __init__(self, max_snapshots: int = 48):
        self._snapshots: List[PatternSnapshot] = []
        self._max = max_snapshots
        self._snapshot_counter = 0

    def store(self, clusters: List[PatternCluster], total_sessions: int) -> str:
        self._snapshot_counter += 1
        snap_id = f"snap-{self._snapshot_counter:04d}"
        snapshot = PatternSnapshot(
            snapshot_id=snap_id,
            created_at=time.time(),
            clusters=clusters,
            total_sessions=total_sessions,
            k=len(clusters),
        )
        if len(self._snapshots) >= self._max:
            self._snapshots.pop(0)
        self._snapshots.append(snapshot)
        return snap_id

    def latest(self) -> Optional[PatternSnapshot]:
        return self._snapshots[-1] if self._snapshots else None

    def assign_cluster(
        self,
        normalized_vector: List[float],
        snapshot: Optional[PatternSnapshot] = None,
    ) -> Optional[PatternCluster]:
        snap = snapshot or self.latest()
        if not snap:
            return None
        return min(
            snap.clusters,
            key=lambda c: _euclidean(normalized_vector, c.centroid),
        )

    def summary(self) -> dict:
        snap = self.latest()
        if not snap:
            return {"snapshots": 0}
        return {
            "snapshot_id": snap.snapshot_id,
            "created_at": snap.created_at,
            "total_sessions": snap.total_sessions,
            "num_clusters": snap.k,
            "clusters": [
                {
                    "id": c.cluster_id,
                    "label": c.label,
                    "size": len(c.member_session_ids),
                    "pct": round(len(c.member_session_ids) / max(snap.total_sessions, 1), 3),
                    "error_rate": c.error_rate,
                    "avg_tokens": c.avg_tokens,
                    "avg_turns": c.avg_turns,
                }
                for c in sorted(snap.clusters, key=lambda c: -len(c.member_session_ids))
            ],
        }
```

## Solution 6: Interaction Pattern Dashboard

```python
import time
from typing import List, Optional


class InteractionPatternDashboard:
    """
    End-to-end pipeline: extract features, normalize, cluster, label, and
    render a ranked pattern report sorted by session frequency.
    Run periodically (e.g., hourly) on recent session records.
    """

    def __init__(
        self,
        extractor: InteractionFeatureExtractor,
        normalizer: FeatureNormalizer,
        clusterer: KMeansPatternClusterer,
        labeler: PatternLabeler,
        registry: PatternRegistry,
    ):
        self._extractor = extractor
        self._normalizer = normalizer
        self._clusterer = clusterer
        self._labeler = labeler
        self._registry = registry

    def run(self, sessions: List[SessionRecord]) -> dict:
        if not sessions:
            return {"error": "no sessions provided"}

        feature_vectors = [self._extractor.extract(s) for s in sessions]
        normalized = self._normalizer.fit_transform(feature_vectors)
        session_ids = [v.session_id for v in feature_vectors]

        clusters = self._clusterer.fit(normalized, session_ids, feature_vectors)
        self._labeler.label_all(clusters)
        snap_id = self._registry.store(clusters, len(sessions))

        summary = self._registry.summary()
        summary["generated_at"] = time.time()
        summary["analysis_window_sessions"] = len(sessions)

        # Highlight high-error clusters
        alerts = [
            {
                "cluster_label": c.label,
                "error_rate": c.error_rate,
                "size": len(c.member_session_ids),
            }
            for c in clusters
            if c.error_rate > 0.25 and len(c.member_session_ids) >= 5
        ]
        summary["error_pattern_alerts"] = alerts
        return summary
```

## Comparison

| Approach | Feature Extraction | Normalization | Clustering | Pattern Labels | Registry |
|---|---|---|---|---|---|
| InteractionFeatureExtractor | Yes | No | No | No | No |
| FeatureNormalizer | No | Yes (Z-score) | No | No | No |
| KMeansPatternClusterer | No | No | Yes (k-means++) | No | No |
| PatternLabeler | No | No | No | Yes | No |
| PatternRegistry | No | No | No | No | Yes (snapshots) |
| InteractionPatternDashboard | Yes | Yes | Yes | Yes | Yes |

**Best for production**: Run `InteractionPatternDashboard.run()` hourly on the past 24 hours of sessions with `k=8–12`. Store snapshots in `PatternRegistry` and compare cluster sizes week-over-week — a new cluster growing from 2% to 15% in one week is an emerging use case or a new failure mode. Use `PatternRegistry.assign_cluster()` in real-time to tag each session with its pattern label, then add that label to your trace spans and metrics. This turns abstract cluster IDs into dimension values on your Prometheus/Datadog dashboards.
