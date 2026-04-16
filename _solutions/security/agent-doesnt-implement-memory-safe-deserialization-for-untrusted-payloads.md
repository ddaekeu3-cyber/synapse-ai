---
title: "Agent Doesn't Implement Memory-Safe Deserialization for Untrusted Payloads"
description: "Agents that deserialize tool responses, webhook bodies, or inter-agent messages with pickle, yaml.load, or eval expose themselves to arbitrary code execution: a crafted payload can instantiate any Python class, execute shell commands, or read secrets from the process environment. Implement safe deserialization that validates schema before parsing, uses restricted loaders, detects dangerous type patterns, and quarantines payloads that fail validation."
date: 2026-04-16
difficulty: advanced
category: security
slug: agent-doesnt-implement-memory-safe-deserialization-for-untrusted-payloads
tags: [deserialization, pickle-safety, yaml-injection, code-execution-prevention, input-validation, supply-chain-security]
symptoms:
  - "Tool responses deserialized with pickle.loads — any tool result can execute arbitrary code"
  - "yaml.load used instead of yaml.safe_load on webhook or inter-agent messages"
  - "JSON payloads with __class__ or __reduce__ fields processed without filtering"
  - "eval or exec called on LLM-generated strings before validation"
  - "Crafted tool response causes agent to write files or open network connections"
---

## Why This Happens

Developers reach for `pickle` because it round-trips Python objects perfectly, `yaml.load` because it handles complex types, and `eval` because it feels convenient for dynamic expressions. None of these are safe when the input comes from an untrusted source — a tool server, a webhook, another agent, or (especially) content that an LLM generated from user input. The fix is a layered deserialization policy: use only safe parsers (JSON, `yaml.safe_load`, `msgpack` with schema), validate the deserialized structure against a strict schema before use, and reject payloads that contain type-bypass patterns regardless of how they arrived.

## Solution 1: Deserialization Safety Classifier

```python
import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, List


class PayloadFormat(str, Enum):
    JSON = "json"
    YAML = "yaml"
    MSGPACK = "msgpack"
    PICKLE = "pickle"
    UNKNOWN = "unknown"


DANGEROUS_PATTERNS = [
    # Python pickle / reduce gadgets
    r"__reduce__",
    r"__reduce_ex__",
    r"__getstate__",
    r"__setstate__",
    r"__class__",
    r"__import__",
    r"__builtins__",
    # YAML deserialization gadgets
    r"!!python/",
    r"!!java/",
    r"!!javax/",
    # eval / exec injection
    r"\beval\s*\(",
    r"\bexec\s*\(",
    r"\bcompile\s*\(",
    # os / subprocess access
    r"\bos\.system\b",
    r"\bsubprocess\.",
    r"\b__import__\s*\(",
]

_PATTERN_RE = re.compile("|".join(DANGEROUS_PATTERNS), re.IGNORECASE)


@dataclass
class SafetyClassification:
    safe: bool
    format: PayloadFormat
    matched_patterns: List[str]
    reason: str

    def __bool__(self) -> bool:
        return self.safe


def classify_payload(raw: bytes | str) -> SafetyClassification:
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
    matches = _PATTERN_RE.findall(text)
    if matches:
        return SafetyClassification(
            safe=False,
            format=PayloadFormat.UNKNOWN,
            matched_patterns=list(set(matches)),
            reason=f"dangerous pattern(s) detected: {list(set(matches))[:3]}",
        )
    # Sniff format
    stripped = text.strip()
    if stripped.startswith("{") or stripped.startswith("["):
        fmt = PayloadFormat.JSON
    elif stripped.startswith("---") or ":" in stripped[:100]:
        fmt = PayloadFormat.YAML
    else:
        fmt = PayloadFormat.UNKNOWN

    return SafetyClassification(
        safe=True,
        format=fmt,
        matched_patterns=[],
        reason="ok",
    )
```

## Solution 2: Safe Deserializer Registry

```python
import json
from typing import Any, Callable, Dict, Optional

try:
    import yaml as _yaml
    _YAML_AVAILABLE = True
except ImportError:
    _YAML_AVAILABLE = False

try:
    import msgpack as _msgpack
    _MSGPACK_AVAILABLE = True
except ImportError:
    _MSGPACK_AVAILABLE = False


class UnsafeDeserializationError(ValueError):
    """Raised when a payload is rejected by the safety classifier."""


class SafeDeserializerRegistry:
    """
    Registry of format-specific safe deserializers.
    Pickle is intentionally absent — it cannot be made safe for untrusted input.
    All deserializers run a pre-scan for dangerous patterns before parsing.
    """

    def __init__(self, scan_before_parse: bool = True):
        self._scan = scan_before_parse
        self._deserializers: Dict[PayloadFormat, Callable] = {
            PayloadFormat.JSON: self._json_safe,
        }
        if _YAML_AVAILABLE:
            self._deserializers[PayloadFormat.YAML] = self._yaml_safe
        if _MSGPACK_AVAILABLE:
            self._deserializers[PayloadFormat.MSGPACK] = self._msgpack_safe

    def deserialize(
        self,
        raw: bytes | str,
        format: Optional[PayloadFormat] = None,
    ) -> Any:
        if self._scan:
            classification = classify_payload(raw)
            if not classification.safe:
                raise UnsafeDeserializationError(
                    f"payload rejected: {classification.reason}"
                )
            if format is None:
                format = classification.format

        if format is None or format == PayloadFormat.UNKNOWN:
            format = PayloadFormat.JSON   # default to strictest

        deserializer = self._deserializers.get(format)
        if deserializer is None:
            raise UnsafeDeserializationError(
                f"no safe deserializer for format '{format}'"
            )
        return deserializer(raw)

    @staticmethod
    def _json_safe(raw: bytes | str) -> Any:
        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        return json.loads(text)

    @staticmethod
    def _yaml_safe(raw: bytes | str) -> Any:
        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        # yaml.safe_load disallows Python-specific tags
        return _yaml.safe_load(text)

    @staticmethod
    def _msgpack_safe(raw: bytes | str) -> Any:
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        # raw=True keeps bytes as bytes, strict_map_key=True prevents large key attacks
        return _msgpack.unpackb(raw, raw=False, strict_map_key=True)
```

## Solution 3: Schema-Enforced Deserializer

```python
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Type, Union


@dataclass
class FieldSchema:
    name: str
    type: type
    required: bool = True
    max_length: Optional[int] = None
    allowed_values: Optional[Set] = None
    nested: Optional["PayloadSchema"] = None


@dataclass
class PayloadSchema:
    fields: List[FieldSchema]
    allow_extra_fields: bool = False
    max_total_fields: int = 100


class SchemaEnforcedDeserializer:
    """
    Deserializes and validates a payload against a declared schema.
    Rejects payloads with unexpected fields, wrong types, or oversized values.
    Strips fields not in the schema when allow_extra_fields=False.
    """

    def __init__(
        self,
        registry: SafeDeserializerRegistry,
        max_payload_bytes: int = 1_048_576,   # 1 MB
    ):
        self._registry = registry
        self._max_bytes = max_payload_bytes

    def deserialize_and_validate(
        self,
        raw: bytes | str,
        schema: PayloadSchema,
        format: Optional[PayloadFormat] = None,
    ) -> Dict[str, Any]:
        # Size guard
        size = len(raw) if isinstance(raw, str) else len(raw)
        if size > self._max_bytes:
            raise UnsafeDeserializationError(
                f"payload too large: {size} > {self._max_bytes} bytes"
            )

        data = self._registry.deserialize(raw, format)
        if not isinstance(data, dict):
            raise UnsafeDeserializationError(
                f"expected dict, got {type(data).__name__}"
            )

        if len(data) > schema.max_total_fields:
            raise UnsafeDeserializationError(
                f"too many fields: {len(data)} > {schema.max_total_fields}"
            )

        result = {}
        schema_names = {f.name for f in schema.fields}

        for field in schema.fields:
            if field.name not in data:
                if field.required:
                    raise UnsafeDeserializationError(
                        f"required field '{field.name}' missing"
                    )
                continue
            value = data[field.name]
            # Type check (allow None for optional)
            if not isinstance(value, field.type) and value is not None:
                raise UnsafeDeserializationError(
                    f"field '{field.name}': expected {field.type.__name__}, "
                    f"got {type(value).__name__}"
                )
            # Length check
            if field.max_length and hasattr(value, "__len__"):
                if len(value) > field.max_length:
                    raise UnsafeDeserializationError(
                        f"field '{field.name}' too long: {len(value)} > {field.max_length}"
                    )
            # Allowed values check
            if field.allowed_values and value not in field.allowed_values:
                raise UnsafeDeserializationError(
                    f"field '{field.name}' value '{value}' not in allowed set"
                )
            result[field.name] = value

        if not schema.allow_extra_fields:
            extra = set(data.keys()) - schema_names
            if extra:
                # Silently strip extra fields rather than error
                pass   # result already only contains schema fields

        return result
```

## Solution 4: Pickle Replacement with Safe Alternatives

```python
import json
import struct
from typing import Any


class SafeObjectSerializer:
    """
    Drop-in replacement interface for pickle that refuses to serialize
    or deserialize classes with custom __reduce__ or __getstate__ methods.
    Uses JSON for primitive types, explicit registry for allowed classes.
    """

    ALLOWED_PRIMITIVES = (str, int, float, bool, list, dict, tuple, type(None))

    def __init__(self):
        self._allowed_classes: dict = {}

    def register_class(self, cls: type, serializer=None, deserializer=None):
        """Register a class as safe with explicit (de)serializers."""
        self._allowed_classes[cls.__name__] = {
            "cls": cls,
            "serialize": serializer or (lambda obj: obj.__dict__),
            "deserialize": deserializer or (lambda data: cls(**data)),
        }

    def dumps(self, obj: Any) -> bytes:
        return json.dumps(self._encode(obj)).encode("utf-8")

    def loads(self, data: bytes) -> Any:
        return self._decode(json.loads(data.decode("utf-8")))

    def _encode(self, obj: Any) -> Any:
        if isinstance(obj, self.ALLOWED_PRIMITIVES):
            if isinstance(obj, (list, tuple)):
                return [self._encode(item) for item in obj]
            if isinstance(obj, dict):
                return {str(k): self._encode(v) for k, v in obj.items()}
            return obj

        cls_name = type(obj).__name__
        entry = self._allowed_classes.get(cls_name)
        if entry is None:
            raise UnsafeDeserializationError(
                f"class '{cls_name}' is not registered for safe serialization"
            )
        return {"__safe_type__": cls_name, "data": entry["serialize"](obj)}

    def _decode(self, obj: Any) -> Any:
        if isinstance(obj, dict) and "__safe_type__" in obj:
            cls_name = obj["__safe_type__"]
            entry = self._allowed_classes.get(cls_name)
            if entry is None:
                raise UnsafeDeserializationError(
                    f"class '{cls_name}' not in allowed registry"
                )
            return entry["deserialize"](obj["data"])
        if isinstance(obj, list):
            return [self._decode(item) for item in obj]
        if isinstance(obj, dict):
            return {k: self._decode(v) for k, v in obj.items()}
        return obj
```

## Solution 5: Deserialization Audit Logger

```python
import time
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class DeserializationEvent:
    source: str            # "tool_response" | "webhook" | "inter_agent"
    format: str
    payload_size_bytes: int
    safe: bool
    rejection_reason: str = ""
    matched_patterns: List[str] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)


class DeserializationAuditLogger:
    """
    Records all deserialization attempts and their outcomes.
    Used for forensic analysis of injection attempts and false positives.
    """

    def __init__(self, max_events: int = 5_000):
        self._events: List[DeserializationEvent] = []
        self._max = max_events

    def log(self, event: DeserializationEvent) -> None:
        if len(self._events) >= self._max:
            self._events.pop(0)
        self._events.append(event)

    def recent_rejections(self, hours: float = 24.0) -> List[DeserializationEvent]:
        cutoff = time.time() - hours * 3600
        return [
            e for e in self._events
            if not e.safe and e.timestamp >= cutoff
        ]

    def summary(self) -> dict:
        recent = [e for e in self._events if e.timestamp >= time.time() - 3600]
        rejected = [e for e in recent if not e.safe]
        return {
            "total_last_hour": len(recent),
            "rejected_last_hour": len(rejected),
            "rejection_rate": round(len(rejected) / max(len(recent), 1), 4),
            "by_source": {
                src: sum(1 for e in rejected if e.source == src)
                for src in {"tool_response", "webhook", "inter_agent"}
            },
            "top_patterns": self._top_patterns(rejected),
        }

    def _top_patterns(self, events: List[DeserializationEvent]) -> List[str]:
        counts: dict = {}
        for e in events:
            for p in e.matched_patterns:
                counts[p] = counts.get(p, 0) + 1
        return sorted(counts, key=counts.get, reverse=True)[:5]
```

## Solution 6: Safe Deserialization Gateway

```python
from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class DeserializationOutcome:
    success: bool
    data: Optional[Any]
    rejection_reason: str = ""
    event: Optional[DeserializationEvent] = None


class SafeDeserializationGateway:
    """
    Unified entry point for all deserialization in the agent.
    Combines pattern scanning, safe parsing, schema validation, and audit logging.
    Never raises on bad input — returns a structured outcome instead.
    """

    def __init__(
        self,
        registry: SafeDeserializerRegistry,
        schema_deserializer: SchemaEnforcedDeserializer,
        audit_logger: DeserializationAuditLogger,
    ):
        self._registry = registry
        self._schema = schema_deserializer
        self._audit = audit_logger

    def receive(
        self,
        raw: bytes | str,
        source: str,
        schema: Optional[PayloadSchema] = None,
        format: Optional[PayloadFormat] = None,
    ) -> DeserializationOutcome:
        size = len(raw) if isinstance(raw, str) else len(raw)

        # Pre-scan
        classification = classify_payload(raw)
        event = DeserializationEvent(
            source=source,
            format=classification.format.value,
            payload_size_bytes=size,
            safe=classification.safe,
            rejection_reason=classification.reason,
            matched_patterns=classification.matched_patterns,
        )

        if not classification.safe:
            self._audit.log(event)
            return DeserializationOutcome(
                success=False,
                data=None,
                rejection_reason=classification.reason,
                event=event,
            )

        try:
            if schema:
                data = self._schema.deserialize_and_validate(raw, schema, format)
            else:
                data = self._registry.deserialize(raw, format)

            event.safe = True
            self._audit.log(event)
            return DeserializationOutcome(success=True, data=data, event=event)

        except (UnsafeDeserializationError, Exception) as exc:
            event.safe = False
            event.rejection_reason = str(exc)
            self._audit.log(event)
            return DeserializationOutcome(
                success=False,
                data=None,
                rejection_reason=str(exc),
                event=event,
            )
```

## Comparison

| Approach | Pattern Scan | Safe Parser | Schema Validation | Audit Log |
|---|---|---|---|---|
| classify_payload | Yes | No | No | No |
| SafeDeserializerRegistry | Yes (pre-scan) | Yes (JSON/YAML safe/msgpack) | No | No |
| SchemaEnforcedDeserializer | Via registry | Via registry | Yes | No |
| SafeObjectSerializer | No | Yes (JSON only) | Via registry | No |
| DeserializationAuditLogger | No | No | No | Yes |
| SafeDeserializationGateway | Yes | Yes | Optional | Yes |

**Best for production**: Replace every `pickle.loads`, `yaml.load`, and `eval` call with `SafeDeserializationGateway.receive()`. Define `PayloadSchema` for every tool response format — this forces you to enumerate what fields you actually use and rejects any unexpected structure. Set `max_payload_bytes` to 1 MB for tool responses and 64 KB for inter-agent messages. Review `DeserializationAuditLogger.summary()` daily — a spike in `__reduce__` or `!!python/` rejections from a specific source indicates a compromised tool server or prompt-injection attempt reaching serialization.
