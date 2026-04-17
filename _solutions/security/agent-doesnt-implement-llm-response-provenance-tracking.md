---
title: "Agent Doesn't Implement LLM Response Provenance Tracking"
description: "Agents that deliver LLM-generated answers without provenance metadata make it impossible to verify which model, which prompt version, and which retrieved sources produced a specific response. When a user disputes an answer, or when a compliance audit requires demonstrating the basis for an AI-generated decision, the provenance chain is unrecoverable. Implement response provenance tracking that records model identity, prompt version, retrieved sources, and tool results for every delivered response."
date: 2026-04-16
difficulty: intermediate
category: security
slug: agent-doesnt-implement-llm-response-provenance-tracking
tags: [provenance, auditability, model-identity, source-attribution, compliance, response-tracing]
symptoms:
  - "Cannot determine which model version generated a disputed response"
  - "No record of which retrieved documents informed a specific answer"
  - "Compliance audit requires source attribution but none is stored"
  - "Prompt version at time of response is unknown — cannot reproduce the conditions"
  - "Responses are stored as plain text with no metadata linking them to their inputs"
---

## Why This Happens

LLM responses are treated as opaque output strings: the model returns text, the agent delivers it, the conversation moves on. No metadata is attached to the response recording what went into producing it. When a user disputes the answer ("the agent told me X, which was wrong"), the only way to investigate is to re-run the same query and hope the model produces the same output. Provenance tracking requires attaching a structured record to every response at generation time — before delivery — so that any response can be traced back to its full production context.

## Solution 1: Response Provenance Record

```python
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class RetrievedSourceRef:
    source_id: str
    source_type: str     # "web", "database", "knowledge_base", "tool"
    title: str
    url_or_path: str
    relevance_score: Optional[float]
    retrieved_at: float
    content_preview: str   # first 200 chars


@dataclass
class ResponseProvenanceRecord:
    response_id: str
    session_id: str
    turn_index: int
    generated_at: float

    # Model identity
    model_id: str
    model_provider: str
    model_temperature: Optional[float]

    # Prompt context
    prompt_version_id: Optional[str]
    system_prompt_fingerprint: str    # SHA-256[:12] of system prompt

    # Input
    user_message_preview: str         # first 300 chars
    input_token_count: int

    # Output
    response_preview: str             # first 300 chars
    output_token_count: int
    finish_reason: str                # "stop" | "length" | "tool_calls"

    # Sources
    retrieved_sources: List[RetrievedSourceRef] = field(default_factory=list)
    tool_calls_made: List[str] = field(default_factory=list)   # tool names in order

    # Metadata
    latency_ms: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)
```

## Solution 2: Provenance Record Builder

```python
import hashlib
import uuid
from typing import Any, Dict, List, Optional


class ProvenanceRecordBuilder:
    """
    Constructs a ResponseProvenanceRecord from the components available
    at response generation time.
    """

    def __init__(
        self,
        model_id: str,
        model_provider: str,
        prompt_version_id: Optional[str] = None,
    ):
        self._model_id = model_id
        self._model_provider = model_provider
        self._prompt_version_id = prompt_version_id

    def build(
        self,
        session_id: str,
        turn_index: int,
        system_prompt: str,
        user_message: str,
        response_text: str,
        input_token_count: int,
        output_token_count: int,
        finish_reason: str,
        latency_ms: float,
        retrieved_sources: Optional[List[RetrievedSourceRef]] = None,
        tool_calls_made: Optional[List[str]] = None,
        temperature: Optional[float] = None,
    ) -> ResponseProvenanceRecord:
        system_fp = hashlib.sha256(system_prompt.strip().encode()).hexdigest()[:12]
        return ResponseProvenanceRecord(
            response_id=uuid.uuid4().hex[:16],
            session_id=session_id,
            turn_index=turn_index,
            generated_at=__import__("time").time(),
            model_id=self._model_id,
            model_provider=self._model_provider,
            model_temperature=temperature,
            prompt_version_id=self._prompt_version_id,
            system_prompt_fingerprint=system_fp,
            user_message_preview=user_message[:300],
            input_token_count=input_token_count,
            response_preview=response_text[:300],
            output_token_count=output_token_count,
            finish_reason=finish_reason,
            retrieved_sources=retrieved_sources or [],
            tool_calls_made=tool_calls_made or [],
            latency_ms=latency_ms,
        )
```

## Solution 3: Provenance Store

```python
import json
import time
from pathlib import Path
from threading import Lock
from typing import Dict, List, Optional


class ResponseProvenanceStore:
    """
    Persists provenance records to a JSONL file and supports
    lookup by response_id and session_id for audit queries.
    """

    def __init__(self, store_path: str, max_in_memory: int = 10000):
        self._path = Path(store_path)
        self._lock = Lock()
        self._index: Dict[str, dict] = {}   # response_id -> record dict
        self._max = max_in_memory

    def save(self, record: ResponseProvenanceRecord) -> None:
        record_dict = self._to_dict(record)
        with self._lock:
            if len(self._index) >= self._max:
                oldest_key = min(self._index, key=lambda k: self._index[k]["generated_at"])
                del self._index[oldest_key]
            self._index[record.response_id] = record_dict
            with self._path.open("a") as f:
                f.write(json.dumps(record_dict) + "\n")

    def get(self, response_id: str) -> Optional[dict]:
        with self._lock:
            return self._index.get(response_id)

    def for_session(self, session_id: str) -> List[dict]:
        with self._lock:
            return [r for r in self._index.values() if r["session_id"] == session_id]

    def recent(self, window_seconds: float = 3600.0) -> List[dict]:
        cutoff = time.time() - window_seconds
        with self._lock:
            return [r for r in self._index.values() if r["generated_at"] >= cutoff]

    @staticmethod
    def _to_dict(r: ResponseProvenanceRecord) -> dict:
        return {
            "response_id": r.response_id,
            "session_id": r.session_id,
            "turn_index": r.turn_index,
            "generated_at": r.generated_at,
            "model_id": r.model_id,
            "model_provider": r.model_provider,
            "model_temperature": r.model_temperature,
            "prompt_version_id": r.prompt_version_id,
            "system_prompt_fingerprint": r.system_prompt_fingerprint,
            "user_message_preview": r.user_message_preview,
            "input_token_count": r.input_token_count,
            "response_preview": r.response_preview,
            "output_token_count": r.output_token_count,
            "finish_reason": r.finish_reason,
            "retrieved_sources": [
                {
                    "source_id": s.source_id,
                    "source_type": s.source_type,
                    "title": s.title,
                    "relevance_score": s.relevance_score,
                    "content_preview": s.content_preview,
                }
                for s in r.retrieved_sources
            ],
            "tool_calls_made": r.tool_calls_made,
            "latency_ms": r.latency_ms,
        }
```

## Solution 4: Provenance Query Engine

```python
from typing import List, Optional


class ProvenanceQueryEngine:
    """
    Supports structured queries against the provenance store for
    audit, dispute resolution, and compliance reporting.
    """

    def __init__(self, store: ResponseProvenanceStore):
        self._store = store

    def explain_response(self, response_id: str) -> dict:
        record = self._store.get(response_id)
        if not record:
            return {"error": f"No provenance record for response '{response_id}'"}

        return {
            "response_id": response_id,
            "generated_at": record["generated_at"],
            "model": f"{record['model_provider']}/{record['model_id']}",
            "prompt_version": record["prompt_version_id"],
            "system_prompt_fingerprint": record["system_prompt_fingerprint"],
            "user_asked": record["user_message_preview"],
            "agent_responded": record["response_preview"],
            "sources_used": len(record["retrieved_sources"]),
            "tools_called": record["tool_calls_made"],
            "input_tokens": record["input_token_count"],
            "output_tokens": record["output_token_count"],
            "latency_ms": record["latency_ms"],
            "sources": record["retrieved_sources"],
        }

    def responses_by_model(
        self, model_id: str, window_seconds: float = 86400.0
    ) -> List[dict]:
        return [
            r for r in self._store.recent(window_seconds)
            if r["model_id"] == model_id
        ]

    def responses_by_prompt_version(
        self, version_id: str, window_seconds: float = 86400.0
    ) -> List[dict]:
        return [
            r for r in self._store.recent(window_seconds)
            if r.get("prompt_version_id") == version_id
        ]
```

## Solution 5: Compliance Evidence Exporter

```python
import json
import time
from typing import List


class ProvenanceComplianceExporter:
    """
    Exports provenance records in a format suitable for compliance audits:
    full source attribution, model identity, and prompt fingerprint for
    each response in a date range.
    """

    def __init__(self, store: ResponseProvenanceStore):
        self._store = store

    def export_session(self, session_id: str) -> dict:
        records = self._store.for_session(session_id)
        return {
            "export_generated_at": time.time(),
            "session_id": session_id,
            "response_count": len(records),
            "responses": sorted(records, key=lambda r: r["turn_index"]),
        }

    def export_window(
        self, window_seconds: float = 86400.0, output_path: Optional[str] = None
    ) -> str:
        records = self._store.recent(window_seconds)
        export = {
            "export_generated_at": time.time(),
            "window_seconds": window_seconds,
            "record_count": len(records),
            "records": sorted(records, key=lambda r: r["generated_at"]),
        }
        result = json.dumps(export, indent=2)
        if output_path:
            with open(output_path, "w") as f:
                f.write(result)
        return result
```

## Solution 6: Provenance Dashboard

```python
import time
from collections import Counter


class ResponseProvenanceDashboard:
    """
    Summarizes provenance metadata across recent responses for
    operational visibility into model usage and source distribution.
    """

    def __init__(
        self,
        store: ResponseProvenanceStore,
        query_engine: ProvenanceQueryEngine,
    ):
        self._store = store
        self._query = query_engine

    def render(self, window_seconds: float = 3600.0) -> dict:
        recent = self._store.recent(window_seconds)
        if not recent:
            return {"window_seconds": window_seconds, "responses": 0}

        model_counts = Counter(r["model_id"] for r in recent)
        version_counts = Counter(
            r.get("prompt_version_id", "unknown") for r in recent
        )
        avg_input = sum(r["input_token_count"] for r in recent) / len(recent)
        avg_output = sum(r["output_token_count"] for r in recent) / len(recent)
        avg_latency = sum(r["latency_ms"] for r in recent) / len(recent)

        return {
            "generated_at": time.time(),
            "window_seconds": window_seconds,
            "total_responses": len(recent),
            "models_used": dict(model_counts),
            "prompt_versions_used": dict(version_counts),
            "avg_input_tokens": round(avg_input, 1),
            "avg_output_tokens": round(avg_output, 1),
            "avg_latency_ms": round(avg_latency, 1),
            "responses_with_sources": sum(
                1 for r in recent if r["retrieved_sources"]
            ),
            "responses_with_tool_calls": sum(
                1 for r in recent if r["tool_calls_made"]
            ),
        }
```

## Comparison

| Approach | Model Identity | Prompt Version | Source Attribution | Dispute Query | Compliance Export |
|---|---|---|---|---|---|
| ResponseProvenanceRecord | Yes | Yes | Yes | No | No |
| ProvenanceRecordBuilder | Yes (from config) | Yes | Yes | No | No |
| ResponseProvenanceStore | No | No | No | No (storage only) | No |
| ProvenanceQueryEngine | Via store | Via store | Via store | Yes | No |
| ProvenanceComplianceExporter | Via store | Via store | Via store | No | Yes |
| ResponseProvenanceDashboard | Via store | Via store | Via store | No | No |

**Best for production**: Attach `response_id` as a header or metadata field in every API response delivered to clients — this allows users and support teams to reference a specific response by ID when filing a dispute. Store provenance records in an append-only store with a minimum 90-day retention for regulated industries. Use `system_prompt_fingerprint` as a compact proxy for prompt version when `prompt_version_id` is not yet implemented — two responses with the same fingerprint ran under identical system prompts, enabling before/after comparisons even without a full version registry.
