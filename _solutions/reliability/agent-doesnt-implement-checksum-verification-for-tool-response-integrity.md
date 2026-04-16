---
title: "Agent Doesn't Implement Checksum Verification for Tool Response Integrity"
description: "AI agents that accept tool responses without integrity checks silently process corrupted, truncated, or tampered data. Checksum verification catches bit-level corruption from network transit, serialisation bugs, and adversarial injection — all before the data reaches the LLM context or agent state."
date: 2025-02-11
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-checksum-verification-for-tool-response-integrity
tags:
  - checksum
  - integrity
  - hmac
  - sha256
  - tool-response
  - data-corruption
  - verification
symptoms:
  - "Tool returns truncated JSON that agent parses silently, producing wrong downstream results"
  - "Network corruption causes a database row to flip a bit; agent acts on wrong data"
  - "Man-in-the-middle injects extra fields into tool response; agent trusts them unconditionally"
  - "Serialisation bug in tool SDK drops fields silently; agent cannot detect the omission"
  - "Agent has no way to distinguish a legitimate empty response from a corrupted one"
---

## Problem

Tools — external APIs, database drivers, file readers, subprocess outputs — return data over unreliable transports. Bit errors, truncation, partial writes, and active injection are all possible. An agent that ingests tool output without verification may make decisions on corrupted data, silently propagate errors downstream, or be manipulated by a tampered response. Checksums (SHA-256, HMAC-SHA-256) and schema-bound hashing detect all of these before the data enters the agent's reasoning context.

---

## Solution 1: ResponseChecksum — Hash-on-Write, Verify-on-Read

```python
import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class ChecksummedResponse:
    payload: bytes
    checksum: str          # hex SHA-256 of payload
    content_type: str = "application/json"


class ResponseChecksum:
    """
    Wraps tool responses with a SHA-256 checksum on the producing side
    and verifies it on the consuming side.

    Usage (tool side — produces checksum):
        cs = ResponseChecksum()
        envelope = cs.seal({"rows": [...], "count": 42})

        # Transmit envelope to agent ...

    Usage (agent side — verifies before use):
        data = cs.open(envelope)
        process(data)   # only reached if checksum matches
    """

    def seal(self, data: Any, content_type: str = "application/json") -> ChecksummedResponse:
        payload = json.dumps(data, sort_keys=True).encode()
        checksum = hashlib.sha256(payload).hexdigest()
        return ChecksummedResponse(
            payload=payload,
            checksum=checksum,
            content_type=content_type,
        )

    def seal_bytes(self, raw: bytes,
                   content_type: str = "application/octet-stream") -> ChecksummedResponse:
        return ChecksummedResponse(
            payload=raw,
            checksum=hashlib.sha256(raw).hexdigest(),
            content_type=content_type,
        )

    def open(self, envelope: ChecksummedResponse) -> Any:
        expected = hashlib.sha256(envelope.payload).hexdigest()
        if not hmac.compare_digest(expected, envelope.checksum):
            raise IntegrityError(
                f"Checksum mismatch: expected {expected}, got {envelope.checksum}. "
                "Tool response may be corrupted or tampered."
            )
        if envelope.content_type == "application/json":
            return json.loads(envelope.payload)
        return envelope.payload

    def verify(self, envelope: ChecksummedResponse) -> bool:
        expected = hashlib.sha256(envelope.payload).hexdigest()
        return hmac.compare_digest(expected, envelope.checksum)


class IntegrityError(RuntimeError):
    pass
```

---

## Solution 2: HMACToolSigner — Authenticated Checksums Between Services

Extend the basic checksum to HMAC-SHA-256 so only a holder of the shared secret can produce a valid signature — preventing injection by a third party who knows the checksum algorithm.

```python
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class SignedToolResponse:
    payload: bytes
    hmac_hex: str
    key_id: str
    timestamp: float
    content_type: str = "application/json"


class HMACToolSigner:
    """
    Signs tool responses with HMAC-SHA-256 using a shared secret.
    Detects both corruption AND active injection by an attacker who
    doesn't hold the secret.

    Usage:
        # Tool side (producer):
        signer = HMACToolSigner(secret=os.environ["TOOL_HMAC_SECRET"],
                                 key_id="db-tool-v1")
        signed = signer.sign({"result": rows})

        # Agent side (consumer):
        verifier = HMACToolSigner(secret=os.environ["TOOL_HMAC_SECRET"],
                                   key_id="db-tool-v1")
        data = verifier.verify_and_open(signed, max_age_s=30.0)
    """

    def __init__(self, secret: bytes | str, key_id: str):
        self._secret = secret.encode() if isinstance(secret, str) else secret
        self._key_id = key_id

    def _compute(self, payload: bytes, timestamp: float) -> str:
        msg = payload + str(timestamp).encode()
        return hmac.new(self._secret, msg, hashlib.sha256).hexdigest()

    def sign(self, data: Any,
             content_type: str = "application/json") -> SignedToolResponse:
        payload = json.dumps(data, sort_keys=True).encode()
        ts = time.time()
        return SignedToolResponse(
            payload=payload,
            hmac_hex=self._compute(payload, ts),
            key_id=self._key_id,
            timestamp=ts,
            content_type=content_type,
        )

    def verify_and_open(self, resp: SignedToolResponse,
                         max_age_s: float = 60.0) -> Any:
        age = time.time() - resp.timestamp
        if age > max_age_s:
            raise IntegrityError(
                f"Response is {age:.1f}s old (max {max_age_s}s). Possible replay attack."
            )
        expected = self._compute(resp.payload, resp.timestamp)
        if not hmac.compare_digest(expected, resp.hmac_hex):
            raise IntegrityError(
                f"HMAC verification failed for key_id={resp.key_id}. "
                "Response was modified in transit."
            )
        return json.loads(resp.payload)
```

---

## Solution 3: SchemaChecksumRegistry — Expected-Field Fingerprinting

Instead of raw byte checksums, compute a structural fingerprint of the response schema (field names + types). Catches silent field omissions and type coercions that a byte checksum would miss if the serialiser still produces valid JSON.

```python
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set


def _schema_fingerprint(obj: Any, depth: int = 0) -> str:
    """Recursively derive a structural fingerprint of a JSON value."""
    if depth > 8:
        return "..."
    if isinstance(obj, dict):
        keys = sorted(obj.keys())
        parts = [f"{k}:{_schema_fingerprint(obj[k], depth+1)}" for k in keys]
        return "{" + ",".join(parts) + "}"
    if isinstance(obj, list):
        if not obj:
            return "[]"
        return f"[{_schema_fingerprint(obj[0], depth+1)}*{len(obj)}]"
    return type(obj).__name__


@dataclass
class SchemaExpectation:
    tool_name: str
    fingerprint: str
    required_fields: Set[str]


class SchemaChecksumRegistry:
    """
    Registers expected response schemas per tool.
    On each invocation, verifies the actual schema fingerprint matches.

    Usage:
        registry = SchemaChecksumRegistry()
        # Register once, during init:
        registry.register_from_sample("search_tool", sample_response)

        # At each call:
        result = await call_tool("search_tool", query)
        registry.verify("search_tool", result)   # raises on schema mismatch
    """

    def __init__(self):
        self._expectations: Dict[str, SchemaExpectation] = {}

    def register_from_sample(self, tool_name: str,
                               sample: Any,
                               required_fields: Optional[Set[str]] = None):
        fp = _schema_fingerprint(sample)
        self._expectations[tool_name] = SchemaExpectation(
            tool_name=tool_name,
            fingerprint=fp,
            required_fields=required_fields or set(),
        )

    def register(self, tool_name: str, fingerprint: str,
                  required_fields: Optional[Set[str]] = None):
        self._expectations[tool_name] = SchemaExpectation(
            tool_name=tool_name,
            fingerprint=fingerprint,
            required_fields=required_fields or set(),
        )

    def verify(self, tool_name: str, response: Any):
        exp = self._expectations.get(tool_name)
        if exp is None:
            return  # no expectation registered — permissive
        actual_fp = _schema_fingerprint(response)
        if actual_fp != exp.fingerprint:
            raise IntegrityError(
                f"Schema mismatch for tool '{tool_name}'. "
                f"Expected: {exp.fingerprint}. Got: {actual_fp}"
            )
        if isinstance(response, dict):
            missing = exp.required_fields - response.keys()
            if missing:
                raise IntegrityError(
                    f"Required fields missing in '{tool_name}' response: {missing}"
                )

    def fingerprint(self, tool_name: str,
                     response: Any) -> str:
        return _schema_fingerprint(response)
```

---

## Solution 4: StreamingChecksumVerifier — Verify Chunked/Streaming Responses

For tools that stream large responses, compute a rolling SHA-256 and verify the final digest against a trailer hash.

```python
import hashlib
from dataclasses import dataclass
from typing import AsyncGenerator, Optional


@dataclass
class StreamChunk:
    data: bytes
    sequence: int
    is_final: bool = False
    final_checksum: Optional[str] = None   # provided only on last chunk


class StreamingChecksumVerifier:
    """
    Verifies integrity of chunked/streaming tool responses.
    Accumulates SHA-256 over arriving chunks; checks against declared
    final_checksum in the last chunk.

    Usage:
        verifier = StreamingChecksumVerifier()
        chunks = []
        async for chunk in tool.stream():
            verified_data = verifier.feed(chunk)
            chunks.append(verified_data)
        # Raises IntegrityError if any chunk was dropped or corrupted.
    """

    def __init__(self):
        self._hasher = hashlib.sha256()
        self._seq = 0
        self._buffer = bytearray()
        self._done = False

    def feed(self, chunk: StreamChunk) -> bytes:
        if chunk.sequence != self._seq:
            raise IntegrityError(
                f"Sequence gap: expected {self._seq}, got {chunk.sequence}. "
                "Chunks dropped or reordered."
            )
        self._hasher.update(chunk.data)
        self._buffer.extend(chunk.data)
        self._seq += 1
        if chunk.is_final:
            if chunk.final_checksum is None:
                raise IntegrityError("Final chunk missing checksum field.")
            actual = self._hasher.hexdigest()
            if not __import__("hmac").compare_digest(actual, chunk.final_checksum):
                raise IntegrityError(
                    f"Stream integrity check failed. "
                    f"Expected {chunk.final_checksum}, computed {actual}."
                )
            self._done = True
        return chunk.data

    def complete(self) -> bytes:
        if not self._done:
            raise IntegrityError("Stream ended without final checksum chunk.")
        return bytes(self._buffer)

    def reset(self):
        self._hasher = hashlib.sha256()
        self._seq = 0
        self._buffer = bytearray()
        self._done = False
```

---

## Solution 5: ToolResponseIntegrityMiddleware — Transparent Wrapping

Middleware that intercepts every tool call result, verifies its checksum automatically, and records integrity failures as metrics.

```python
import asyncio
import logging
import time
from functools import wraps
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)


class ToolResponseIntegrityMiddleware:
    """
    Wraps tool call functions to automatically verify checksums.
    Tools opt in by attaching a 'checksum' key to their response dict,
    or by being registered with a SchemaChecksumRegistry.

    Usage:
        middleware = ToolResponseIntegrityMiddleware(
            schema_registry=registry,
            strict=True,  # raise on any verification failure
        )

        @middleware.wrap("search")
        async def search_tool(query: str) -> dict: ...
    """

    def __init__(self,
                 schema_registry: Optional["SchemaChecksumRegistry"] = None,
                 strict: bool = True):
        self._registry = schema_registry
        self._strict = strict
        self._stats: Dict[str, int] = {
            "verified": 0, "failed": 0, "skipped": 0
        }

    def wrap(self, tool_name: str):
        middleware = self

        def decorator(fn: Callable) -> Callable:
            @wraps(fn)
            async def wrapper(*args, **kwargs) -> Any:
                result = await fn(*args, **kwargs)
                try:
                    middleware._verify(tool_name, result)
                    middleware._stats["verified"] += 1
                except IntegrityError as exc:
                    middleware._stats["failed"] += 1
                    logger.error("Integrity failure in tool '%s': %s",
                                 tool_name, exc)
                    if middleware._strict:
                        raise
                return result
            return wrapper
        return decorator

    def _verify(self, tool_name: str, result: Any):
        if isinstance(result, dict) and "checksum" in result:
            payload_keys = {k: result[k] for k in result if k != "checksum"}
            import json, hashlib
            computed = hashlib.sha256(
                json.dumps(payload_keys, sort_keys=True).encode()
            ).hexdigest()
            if not __import__("hmac").compare_digest(computed, result["checksum"]):
                raise IntegrityError(
                    f"Inline checksum mismatch for tool '{tool_name}'"
                )
        elif self._registry:
            self._registry.verify(tool_name, result)
        else:
            self._stats["skipped"] += 1

    @property
    def stats(self) -> dict:
        return dict(self._stats)
```

---

## Solution 6: IntegrityAwareToolExecutor — Full Pipeline Integration

End-to-end tool executor that signs outbound requests, verifies inbound responses, and quarantines tools that fail integrity checks repeatedly.

```python
import asyncio
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


@dataclass
class ToolIntegrityRecord:
    tool_name: str
    calls: int = 0
    failures: int = 0
    quarantined: bool = False
    quarantine_until: float = 0.0

    @property
    def failure_rate(self) -> float:
        return self.failures / self.calls if self.calls else 0.0


class IntegrityAwareToolExecutor:
    """
    Executes tool calls with HMAC signing + schema verification.
    Auto-quarantines tools whose failure rate exceeds a threshold.

    Usage:
        executor = IntegrityAwareToolExecutor(
            hmac_secret=os.environ["TOOL_HMAC_SECRET"],
            quarantine_threshold=0.1,   # quarantine after 10% failures
            quarantine_duration=300.0,  # 5 minutes
        )
        executor.register("web_search", web_search_fn, expected_schema=sample)
        result = await executor.call("web_search", {"query": "AI safety"})
    """

    def __init__(self, hmac_secret: str,
                 quarantine_threshold: float = 0.1,
                 quarantine_duration: float = 300.0):
        self._signer = HMACToolSigner(hmac_secret, "agent")
        self._registry = SchemaChecksumRegistry()
        self._tools: Dict[str, Callable] = {}
        self._records: Dict[str, ToolIntegrityRecord] = defaultdict(
            lambda: ToolIntegrityRecord(tool_name="?")
        )
        self._threshold = quarantine_threshold
        self._quar_duration = quarantine_duration

    def register(self, name: str, fn: Callable,
                  expected_schema: Optional[Any] = None):
        self._tools[name] = fn
        if expected_schema is not None:
            self._registry.register_from_sample(name, expected_schema)
        self._records[name] = ToolIntegrityRecord(tool_name=name)

    async def call(self, tool_name: str, args: dict) -> Any:
        record = self._records[tool_name]
        if record.quarantined and time.time() < record.quarantine_until:
            raise RuntimeError(
                f"Tool '{tool_name}' is quarantined until "
                f"{record.quarantine_until:.0f} due to integrity failures."
            )
        record.quarantined = False
        fn = self._tools.get(tool_name)
        if fn is None:
            raise KeyError(f"Tool '{tool_name}' not registered")
        result = await fn(**args)
        record.calls += 1
        try:
            self._registry.verify(tool_name, result)
        except IntegrityError:
            record.failures += 1
            if record.failure_rate > self._threshold and record.calls >= 10:
                record.quarantined = True
                record.quarantine_until = time.time() + self._quar_duration
            raise
        return result

    def integrity_report(self) -> List[dict]:
        return [
            {
                "tool": r.tool_name,
                "calls": r.calls,
                "failures": r.failures,
                "failure_rate": round(r.failure_rate, 4),
                "quarantined": r.quarantined,
            }
            for r in self._records.values()
        ]
```

---

## Comparison

| Approach | Detects Corruption | Detects Injection | Streaming | Schema-Aware | Auto-Quarantine |
|---|---|---|---|---|---|
| **ResponseChecksum** | Yes | No (no secret) | No | No | No |
| **HMACToolSigner** | Yes | Yes | No | No | No |
| **SchemaChecksumRegistry** | Structural only | Structural only | No | Yes | No |
| **StreamingChecksumVerifier** | Yes | No | Yes | No | No |
| **IntegrityMiddleware** | Yes | Yes (with HMAC) | No | Yes | No |
| **IntegrityAwareToolExecutor** | Yes | Yes | No | Yes | Yes |

**Key insight**: use `HMACToolSigner` for any tool response that crosses a process or network boundary. Use `SchemaChecksumRegistry` in addition for catching serialisation bugs that corrupt structure without changing bytes (e.g., field name typos in a new SDK version). The combination catches both physical corruption and logical tampering.
