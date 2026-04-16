---
title: "Agent Doesn't Implement Differential Privacy for User Data"
description: "How to apply differential privacy techniques — Laplace noise, Gaussian mechanisms, local DP, and privacy budgets — to protect user data in AI agent systems while preserving analytical utility."
date: 2025-01-16
difficulty: advanced
category: security
slug: agent-doesnt-implement-differential-privacy-for-user-data
tags:
  - security
  - privacy
  - differential-privacy
  - data-protection
  - noise-mechanisms
  - privacy-budget
  - gdpr
symptoms:
  - "Agent logs or stores raw user queries enabling individual re-identification"
  - "Aggregate statistics over user data leak information about specific users"
  - "Fine-tuning or embedding training data exposes individual conversations"
  - "Usage analytics can be correlated to identify specific users"
  - "No formal privacy guarantee on what a third party can learn from agent outputs"
  - "Regulatory compliance requires provable privacy bounds (GDPR, CCPA)"
---

## Why This Happens

AI agents collect user queries, conversation history, tool call logs, and usage patterns. Even with PII masking, aggregate statistics and model fine-tuning data can enable membership inference attacks — allowing an adversary to determine whether a specific individual's data was included in a dataset. Differential privacy (DP) provides a *mathematical guarantee*: any single individual's data changes the output distribution by at most a bounded factor ε, making individual contributions statistically indistinguishable.

Without DP, agents that expose aggregate query statistics, train on user conversations, or return analytics over user pools risk violating privacy regulations and user trust — even if no single field looks sensitive in isolation.

---

## Solution 1: Laplace Mechanism for Numeric Aggregates

The Laplace mechanism adds calibrated noise to numeric query results. Sensitivity = max change any one record can cause; noise scale = sensitivity / ε.

```python
import math
import numpy as np
from typing import Callable, Any

class LaplaceMechanism:
    """
    Differentially private numeric aggregation using the Laplace mechanism.
    Provides ε-differential privacy where smaller ε = stronger privacy.
    """

    def __init__(self, epsilon: float):
        if epsilon <= 0:
            raise ValueError("epsilon must be positive")
        self.epsilon = epsilon

    def add_noise(self, true_value: float, sensitivity: float) -> float:
        """
        Add Laplace noise calibrated to the query sensitivity.
        sensitivity = max |f(D) - f(D')| over all neighboring datasets D, D'
        """
        scale = sensitivity / self.epsilon
        noise = np.random.laplace(0, scale)
        return true_value + noise

    def count(self, values: list, predicate: Callable[[Any], bool] | None = None) -> float:
        """DP count query. Sensitivity = 1 (adding/removing one record changes count by 1)."""
        raw = sum(1 for v in values if predicate is None or predicate(v))
        return max(0.0, self.add_noise(raw, sensitivity=1.0))

    def mean(self, values: list[float], lower: float, upper: float) -> float:
        """
        DP mean with bounded sensitivity.
        Clamps values to [lower, upper], sensitivity = (upper - lower) / n.
        """
        if not values:
            return self.add_noise(0.0, sensitivity=(upper - lower))
        clamped = [max(lower, min(upper, v)) for v in values]
        n = len(clamped)
        raw_mean = sum(clamped) / n
        sensitivity = (upper - lower) / n
        return self.add_noise(raw_mean, sensitivity)

    def sum(self, values: list[float], lower: float, upper: float) -> float:
        """DP sum. Sensitivity = upper - lower (one record contributes at most this much)."""
        clamped = [max(lower, min(upper, v)) for v in values]
        raw_sum = sum(clamped)
        return self.add_noise(raw_sum, sensitivity=(upper - lower))

    def histogram(
        self,
        values: list,
        bins: list,
        sensitivity: float = 1.0,
    ) -> dict[str, float]:
        """DP histogram. Each bin count has sensitivity 1."""
        counts: dict[str, int] = {str(b): 0 for b in bins}
        for v in values:
            if str(v) in counts:
                counts[str(v)] += 1
        return {
            bucket: max(0.0, self.add_noise(count, sensitivity))
            for bucket, count in counts.items()
        }


# --- Usage: DP analytics over user query logs ---

def compute_dp_usage_stats(user_queries: list[dict], epsilon: float = 1.0) -> dict:
    mech = LaplaceMechanism(epsilon=epsilon)
    response_times = [q["response_ms"] for q in user_queries]
    error_flags = [q.get("is_error", False) for q in user_queries]

    return {
        "dp_total_queries": mech.count(user_queries),
        "dp_mean_response_ms": mech.mean(response_times, lower=0, upper=30_000),
        "dp_error_rate": mech.count(user_queries, lambda q: q.get("is_error", False)) / max(1, len(user_queries)),
        "dp_model_histogram": mech.histogram(
            [q.get("model", "unknown") for q in user_queries],
            bins=["claude-3", "claude-2", "gpt-4", "other"],
        ),
        "epsilon_spent": epsilon,
    }
```

---

## Solution 2: Gaussian Mechanism for (ε, δ)-DP

The Gaussian mechanism provides (ε, δ)-differential privacy — a slightly relaxed guarantee that allows more accurate answers in exchange for a small failure probability δ.

```python
class GaussianMechanism:
    """
    (ε, δ)-differentially private mechanism using Gaussian noise.
    More accurate than Laplace for the same ε when δ > 0.
    """

    def __init__(self, epsilon: float, delta: float):
        if epsilon <= 0 or delta <= 0 or delta >= 1:
            raise ValueError("epsilon > 0 and 0 < delta < 1 required")
        self.epsilon = epsilon
        self.delta = delta

    def _sigma(self, sensitivity: float) -> float:
        """
        Compute noise standard deviation using the analytic Gaussian mechanism.
        σ = sensitivity * sqrt(2 * ln(1.25/δ)) / ε
        """
        return sensitivity * math.sqrt(2 * math.log(1.25 / self.delta)) / self.epsilon

    def add_noise(self, true_value: float, sensitivity: float) -> float:
        sigma = self._sigma(sensitivity)
        return true_value + np.random.normal(0, sigma)

    def add_noise_vector(self, values: np.ndarray, sensitivity: float) -> np.ndarray:
        """Add Gaussian noise to an entire vector (e.g., an embedding)."""
        # L2 sensitivity for vectors
        sigma = self._sigma(sensitivity)
        return values + np.random.normal(0, sigma, size=values.shape)

    def privatize_embedding(
        self,
        embedding: list[float],
        l2_sensitivity: float = 1.0,
    ) -> list[float]:
        """
        Privatize a user query embedding before storing or training on it.
        l2_sensitivity should match the embedding normalization.
        """
        vec = np.array(embedding, dtype=np.float64)
        # Normalize to unit sphere (bounds L2 sensitivity)
        norm = np.linalg.norm(vec)
        if norm > 0:
            vec = vec / norm
        noisy = self.add_noise_vector(vec, sensitivity=l2_sensitivity)
        return noisy.tolist()


# --- Privatize query embeddings before storage ---

class PrivacyPreservingEmbeddingStore:
    def __init__(self, mechanism: GaussianMechanism):
        self.mech = mechanism
        self._store: dict[str, list[float]] = {}

    def store_query_embedding(self, query_id: str, embedding: list[float]) -> None:
        """Store a DP-noised embedding — original query is not recoverable."""
        private_embedding = self.mech.privatize_embedding(embedding)
        self._store[query_id] = private_embedding

    def get_embedding(self, query_id: str) -> list[float] | None:
        return self._store.get(query_id)
```

---

## Solution 3: Local Differential Privacy (LDP)

In local DP, noise is added *on the client side* before any data leaves the user's context — the server never sees the true value. This is stronger than central DP but less accurate.

```python
import random
import math

class LocalDifferentialPrivacy:
    """
    Local differential privacy mechanisms.
    Data is randomized before leaving the user's agent instance.
    The central server receives only noised reports.
    """

    def __init__(self, epsilon: float):
        self.epsilon = epsilon

    def randomized_response(self, true_value: bool) -> bool:
        """
        Classic randomized response for binary attributes.
        With prob p = e^ε / (e^ε + 1) report truth; else flip.
        Provides ε-LDP.
        """
        p = math.exp(self.epsilon) / (math.exp(self.epsilon) + 1)
        if random.random() < p:
            return true_value
        return not true_value

    def unary_encoding(self, value: int, domain_size: int) -> list[int]:
        """
        Unary encoding + randomized response for categorical values.
        Each bit is independently flipped with calibrated probability.
        """
        p = math.exp(self.epsilon / 2) / (math.exp(self.epsilon / 2) + 1)
        q = 1 - p
        # Encode: true bit for value's position
        encoded = [1 if i == value else 0 for i in range(domain_size)]
        # Randomize each bit
        return [
            1 if (bit == 1 and random.random() < p) or (bit == 0 and random.random() < q)
            else 0
            for bit in encoded
        ]

    def laplace_ldp(self, true_value: float, sensitivity: float, lower: float, upper: float) -> float:
        """
        LDP version: clamp + add Laplace noise locally.
        """
        clamped = max(lower, min(upper, true_value))
        scale = sensitivity / self.epsilon
        return clamped + np.random.laplace(0, scale)

    def privatize_feature(self, feature_name: str, value: Any, feature_type: str, **kwargs) -> Any:
        """Dispatch to appropriate LDP mechanism by feature type."""
        if feature_type == "boolean":
            return self.randomized_response(bool(value))
        elif feature_type == "categorical":
            domain = kwargs["domain"]
            idx = domain.index(value) if value in domain else 0
            return self.unary_encoding(idx, len(domain))
        elif feature_type == "numeric":
            return self.laplace_ldp(
                float(value),
                kwargs.get("sensitivity", 1.0),
                kwargs.get("lower", 0.0),
                kwargs.get("upper", 1.0),
            )
        raise ValueError(f"Unknown feature type: {feature_type}")


# --- Agent-side LDP report before telemetry upload ---

class LDPTelemetryReporter:
    def __init__(self, epsilon: float = 2.0):
        self.ldp = LocalDifferentialPrivacy(epsilon)

    def build_private_report(self, session: dict) -> dict:
        """
        Build a privatized telemetry report.
        Raw session data never leaves the agent process.
        """
        return {
            "used_code_tool": self.ldp.randomized_response(session.get("used_code_tool", False)),
            "error_occurred": self.ldp.randomized_response(session.get("error_occurred", False)),
            "model_category": self.ldp.unary_encoding(
                ["fast", "balanced", "powerful"].index(session.get("model_category", "balanced")),
                domain_size=3,
            ),
            "response_time_bucket": self.ldp.laplace_ldp(
                session.get("response_time_bucket", 0),
                sensitivity=1.0, lower=0.0, upper=5.0,
            ),
            "epsilon": self.ldp.epsilon,
        }
```

---

## Solution 4: Privacy Budget Manager with Composition Tracking

Multiple DP queries on the same dataset compound privacy loss. A budget manager tracks ε expenditure and refuses queries that would exceed the total budget.

```python
from dataclasses import dataclass, field
import threading

@dataclass
class PrivacyBudget:
    total_epsilon: float
    total_delta: float = 1e-5
    spent_epsilon: float = 0.0
    spent_delta: float = 0.0
    query_log: list[dict] = field(default_factory=list)

    @property
    def remaining_epsilon(self) -> float:
        return self.total_epsilon - self.spent_epsilon

    @property
    def is_exhausted(self) -> bool:
        return self.spent_epsilon >= self.total_epsilon

class PrivacyBudgetManager:
    """
    Tracks cumulative privacy loss across multiple DP queries.
    Uses basic composition theorem: total ε = sum of individual εs.
    Raises BudgetExhaustedError when budget is exceeded.
    """

    def __init__(self, total_epsilon: float, total_delta: float = 1e-5):
        self._budgets: dict[str, PrivacyBudget] = {}
        self._default_epsilon = total_epsilon
        self._default_delta = total_delta
        self._lock = threading.Lock()

    def get_or_create_budget(self, subject_id: str) -> PrivacyBudget:
        with self._lock:
            if subject_id not in self._budgets:
                self._budgets[subject_id] = PrivacyBudget(
                    total_epsilon=self._default_epsilon,
                    total_delta=self._default_delta,
                )
            return self._budgets[subject_id]

    def request_budget(
        self,
        subject_id: str,
        epsilon_needed: float,
        delta_needed: float = 0.0,
        query_name: str = "",
    ) -> bool:
        """
        Attempt to consume epsilon_needed from subject's budget.
        Returns True if approved; False if budget exhausted.
        """
        with self._lock:
            budget = self.get_or_create_budget(subject_id)
            if budget.spent_epsilon + epsilon_needed > budget.total_epsilon:
                return False
            if budget.spent_delta + delta_needed > budget.total_delta:
                return False
            budget.spent_epsilon += epsilon_needed
            budget.spent_delta += delta_needed
            budget.query_log.append({
                "query": query_name,
                "epsilon": epsilon_needed,
                "delta": delta_needed,
                "cumulative_epsilon": budget.spent_epsilon,
                "ts": time.time(),
            })
            return True

    def get_budget_status(self, subject_id: str) -> dict:
        budget = self.get_or_create_budget(subject_id)
        return {
            "subject_id": subject_id,
            "total_epsilon": budget.total_epsilon,
            "spent_epsilon": budget.spent_epsilon,
            "remaining_epsilon": budget.remaining_epsilon,
            "exhausted": budget.is_exhausted,
            "query_count": len(budget.query_log),
        }

    def reset_budget(self, subject_id: str) -> None:
        """Reset budget (e.g., at start of a new time window)."""
        with self._lock:
            if subject_id in self._budgets:
                b = self._budgets[subject_id]
                self._budgets[subject_id] = PrivacyBudget(
                    total_epsilon=b.total_epsilon,
                    total_delta=b.total_delta,
                )

class BudgetExhaustedError(Exception):
    pass

class BudgetAwareDPQuery:
    """Wraps DP queries with automatic budget tracking."""

    def __init__(self, budget_manager: PrivacyBudgetManager):
        self.bm = budget_manager

    def count(self, subject_id: str, values: list, epsilon: float = 0.5) -> float:
        if not self.bm.request_budget(subject_id, epsilon, query_name="count"):
            raise BudgetExhaustedError(f"Privacy budget exhausted for {subject_id}")
        mech = LaplaceMechanism(epsilon)
        return mech.count(values)

    def mean(self, subject_id: str, values: list[float], lower: float, upper: float, epsilon: float = 0.5) -> float:
        if not self.bm.request_budget(subject_id, epsilon, query_name="mean"):
            raise BudgetExhaustedError(f"Privacy budget exhausted for {subject_id}")
        mech = LaplaceMechanism(epsilon)
        return mech.mean(values, lower, upper)
```

---

## Solution 5: DP Fine-Tuning Data Sanitizer

Before using conversation logs for model fine-tuning, sanitize the dataset with DP noise to bound individual contribution.

```python
import hashlib
from collections import defaultdict

class DPFineTuningDataset:
    """
    Prepares a fine-tuning dataset with differential privacy guarantees.
    Limits per-user contribution (user-level DP) and adds noise.
    """

    def __init__(
        self,
        epsilon: float = 2.0,
        delta: float = 1e-5,
        max_records_per_user: int = 5,
    ):
        self.epsilon = epsilon
        self.delta = delta
        self.max_per_user = max_records_per_user
        self.mech = GaussianMechanism(epsilon, delta)

    def _user_id(self, record: dict) -> str:
        """Extract or derive a user identifier from a record."""
        return record.get("user_id") or hashlib.md5(
            record.get("session_id", "unknown").encode()
        ).hexdigest()[:8]

    def subsample_and_clip(self, records: list[dict]) -> list[dict]:
        """
        Apply user-level DP:
        1. Cap each user's contribution to max_records_per_user
        2. Shuffle to prevent order-based attacks
        """
        by_user: dict[str, list[dict]] = defaultdict(list)
        for r in records:
            by_user[self._user_id(r)].append(r)

        clipped = []
        for user_id, user_records in by_user.items():
            # Randomly sample max_per_user records per user
            sampled = random.sample(user_records, min(len(user_records), self.max_per_user))
            clipped.extend(sampled)

        random.shuffle(clipped)
        return clipped

    def privatize_text_features(self, record: dict) -> dict:
        """
        Strip identifying fields and optionally add noise to numeric features.
        """
        private = {k: v for k, v in record.items() if k not in (
            "user_id", "session_id", "ip_address", "device_id", "email"
        )}
        # Privatize numeric fields
        if "response_length" in private:
            private["response_length"] = int(self.mech.add_noise(
                private["response_length"], sensitivity=100.0
            ))
        if "latency_ms" in private:
            private["latency_ms"] = max(0, int(self.mech.add_noise(
                private["latency_ms"], sensitivity=500.0
            )))
        return private

    def prepare_dataset(self, raw_records: list[dict]) -> tuple[list[dict], dict]:
        """
        Full pipeline: subsample -> clip -> privatize.
        Returns (privatized_records, metadata).
        """
        clipped = self.subsample_and_clip(raw_records)
        private_records = [self.privatize_text_features(r) for r in clipped]

        metadata = {
            "original_count": len(raw_records),
            "after_clipping": len(clipped),
            "epsilon": self.epsilon,
            "delta": self.delta,
            "max_per_user": self.max_per_user,
            "privacy_guarantee": f"({self.epsilon}, {self.delta})-DP",
        }
        return private_records, metadata
```

---

## Solution 6: DP Query API Facade

A unified facade that enforces privacy policies on all data access, with per-requester budget isolation.

```python
from enum import Enum
from typing import Optional

class PrivacyTier(Enum):
    PUBLIC     = ("public",    10.0, 1e-3)   # Loose privacy for aggregate dashboards
    INTERNAL   = ("internal",  2.0,  1e-5)   # Internal analytics
    RESEARCH   = ("research",  1.0,  1e-6)   # External researchers
    REGULATORY = ("regulatory", 0.1, 1e-8)   # Regulatory / audit reports

    def __init__(self, label: str, epsilon: float, delta: float):
        self.label = label
        self.epsilon = epsilon
        self.delta = delta

class DPQueryFacade:
    """
    Single entry point for all analytics queries over user data.
    Enforces privacy tier + budget constraints on every call.
    """

    def __init__(self, data_store, budget_manager: PrivacyBudgetManager):
        self.store = data_store
        self.bm = budget_manager

    def query(
        self,
        requester_id: str,
        query_type: str,
        params: dict,
        tier: PrivacyTier = PrivacyTier.INTERNAL,
    ) -> dict:
        epsilon = tier.epsilon
        if not self.bm.request_budget(requester_id, epsilon, delta_needed=tier.delta, query_name=query_type):
            raise BudgetExhaustedError(
                f"Requester '{requester_id}' has exhausted their {tier.label} privacy budget"
            )

        mech = LaplaceMechanism(epsilon)
        raw_data = self.store.fetch(query_type, params)

        if query_type == "count":
            result = mech.count(raw_data)
        elif query_type == "mean":
            result = mech.mean(raw_data, params["lower"], params["upper"])
        elif query_type == "histogram":
            result = mech.histogram(raw_data, params["bins"])
        else:
            raise ValueError(f"Unknown query type: {query_type}")

        return {
            "result": result,
            "privacy_tier": tier.label,
            "epsilon_used": epsilon,
            "budget_remaining": self.bm.get_budget_status(requester_id)["remaining_epsilon"],
        }
```

---

## Comparison

| Solution | Privacy Model | Noise Location | Accuracy | Best For |
|---|---|---|---|---|
| Laplace Mechanism | ε-DP (central) | Server-side | High | Count/sum/mean analytics |
| Gaussian Mechanism | (ε,δ)-DP (central) | Server-side | Higher | Embedding privatization |
| Local DP | ε-LDP (local) | Client-side | Lower | Strongest privacy guarantees |
| Privacy Budget Manager | Composition | N/A (tracking) | N/A | Multi-query budget control |
| DP Fine-Tuning Dataset | User-level DP | Pre-training | Moderate | Training data preparation |
| DP Query Facade | Tiered DP | Server-side | Tier-dependent | Unified analytics API |

**Choose Laplace** for count/aggregate analytics dashboards. **Choose Gaussian** when privatizing high-dimensional vectors like embeddings. **Choose local DP** when you cannot trust the central server or need the strongest privacy claim. **Always use a budget manager** whenever multiple queries touch the same user dataset — naive composition quickly exhausts meaningful privacy guarantees. **Apply the fine-tuning sanitizer** before any user conversation data is used for model training.
