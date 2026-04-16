---
title: "Agent Doesn't Implement Server-Side Request Validation Before LLM Invocation"
description: "AI agents that forward raw client requests directly to the LLM without server-side validation allow clients to inject oversized payloads, malformed JSON, disallowed roles, or out-of-range parameters that the LLM will faithfully execute. Pre-invocation validation enforces a strict schema on every request field before any token is consumed, blocking malformed and malicious inputs at the API boundary."
date: 2025-02-15
difficulty: intermediate
category: security
slug: agent-doesnt-implement-server-side-request-validation-before-llm-invocation
tags:
  - input-validation
  - request-validation
  - pre-invocation
  - schema
  - security
  - api-boundary
  - llm-security
symptoms:
  - "Client sends 500,000-token messages that pass through to the LLM unchecked"
  - "Malformed JSON in the message array causes a 500 error from the LLM provider"
  - "Client injects a 'system' role message into the messages array"
  - "Out-of-range temperature (e.g., 999.0) passed directly to the LLM API"
  - "No validation layer exists between the HTTP endpoint and the LLM SDK call"
---

## Problem

Client-controlled fields — `messages`, `model`, `temperature`, `max_tokens`, `system` — must be validated and sanitised before they reach the LLM API. Without validation, clients can: inject system-role messages that override the server's system prompt; send payloads that exhaust token budgets; pass model names not on the approved list; or supply parameters outside valid ranges. Server-side validation runs synchronously at the request boundary, rejects invalid input before any LLM cost is incurred, and returns structured error responses that do not leak internal schema details.

---

## Solution 1: LLMRequestValidator — Strict Schema Validation

```python
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set


@dataclass
class ValidationError:
    field: str
    message: str


@dataclass
class LLMRequest:
    messages: List[Dict[str, Any]]
    model: str = "claude-sonnet-4-6"
    max_tokens: int = 1024
    temperature: float = 1.0
    system: Optional[str] = None
    stream: bool = False


@dataclass
class ValidationResult:
    valid: bool
    errors: List[ValidationError] = field(default_factory=list)
    sanitised: Optional[LLMRequest] = None


class LLMRequestValidator:
    """
    Validates and sanitises LLM request parameters before invocation.
    Rejects disallowed models, out-of-range parameters, oversized payloads,
    and invalid message roles.

    Usage:
        validator = LLMRequestValidator(
            allowed_models={"claude-sonnet-4-6", "claude-haiku-4-5-20251001"},
            max_message_chars=50_000,
            max_messages=50,
        )
        result = validator.validate(raw_request_dict)
        if not result.valid:
            return 400, [e.message for e in result.errors]
        response = await llm_client.invoke(result.sanitised)
    """

    ALLOWED_ROLES = frozenset({"user", "assistant"})
    MAX_TEMPERATURE = 2.0
    MIN_TEMPERATURE = 0.0
    MAX_MAX_TOKENS = 8192
    MIN_MAX_TOKENS = 1

    def __init__(self, allowed_models: Optional[Set[str]] = None,
                  max_message_chars: int = 100_000,
                  max_messages: int = 100,
                  max_system_chars: int = 10_000):
        self._allowed_models = allowed_models or {
            "claude-sonnet-4-6",
            "claude-haiku-4-5-20251001",
            "claude-opus-4-6",
        }
        self._max_msg_chars = max_message_chars
        self._max_messages = max_messages
        self._max_system_chars = max_system_chars

    def validate(self, raw: Dict[str, Any]) -> ValidationResult:
        errors: List[ValidationError] = []

        # messages
        messages = raw.get("messages")
        if not isinstance(messages, list):
            errors.append(ValidationError("messages", "must be an array"))
        elif len(messages) == 0:
            errors.append(ValidationError("messages", "must not be empty"))
        elif len(messages) > self._max_messages:
            errors.append(ValidationError(
                "messages", f"exceeds max {self._max_messages} messages"
            ))
        else:
            errors.extend(self._validate_messages(messages))

        # model
        model = raw.get("model", "claude-sonnet-4-6")
        if not isinstance(model, str) or model not in self._allowed_models:
            errors.append(ValidationError(
                "model",
                f"'{model}' is not an allowed model. "
                f"Allowed: {sorted(self._allowed_models)}"
            ))
            model = "claude-sonnet-4-6"

        # max_tokens
        max_tokens = raw.get("max_tokens", 1024)
        if not isinstance(max_tokens, int) or not (
            self.MIN_MAX_TOKENS <= max_tokens <= self.MAX_MAX_TOKENS
        ):
            errors.append(ValidationError(
                "max_tokens",
                f"must be integer between {self.MIN_MAX_TOKENS} and {self.MAX_MAX_TOKENS}"
            ))
            max_tokens = 1024

        # temperature
        temperature = raw.get("temperature", 1.0)
        if not isinstance(temperature, (int, float)) or not (
            self.MIN_TEMPERATURE <= float(temperature) <= self.MAX_TEMPERATURE
        ):
            errors.append(ValidationError(
                "temperature",
                f"must be float between {self.MIN_TEMPERATURE} and {self.MAX_TEMPERATURE}"
            ))
            temperature = 1.0

        # system
        system = raw.get("system")
        if system is not None:
            if not isinstance(system, str):
                errors.append(ValidationError("system", "must be a string"))
                system = None
            elif len(system) > self._max_system_chars:
                errors.append(ValidationError(
                    "system", f"exceeds max {self._max_system_chars} chars"
                ))
                system = None

        if errors:
            return ValidationResult(valid=False, errors=errors)

        return ValidationResult(
            valid=True,
            sanitised=LLMRequest(
                messages=messages,
                model=model,
                max_tokens=max_tokens,
                temperature=float(temperature),
                system=system,
                stream=bool(raw.get("stream", False)),
            ),
        )

    def _validate_messages(self, messages: list) -> List[ValidationError]:
        errors = []
        total_chars = 0
        for i, msg in enumerate(messages):
            if not isinstance(msg, dict):
                errors.append(ValidationError(
                    f"messages[{i}]", "must be an object"
                ))
                continue
            role = msg.get("role")
            if role not in self.ALLOWED_ROLES:
                errors.append(ValidationError(
                    f"messages[{i}].role",
                    f"'{role}' is not allowed. Allowed roles: {sorted(self.ALLOWED_ROLES)}"
                ))
            content = msg.get("content", "")
            if not isinstance(content, (str, list)):
                errors.append(ValidationError(
                    f"messages[{i}].content", "must be string or array"
                ))
            else:
                text_len = len(content) if isinstance(content, str) else sum(
                    len(c.get("text", "")) for c in content
                    if isinstance(c, dict)
                )
                total_chars += text_len
                if total_chars > self._max_msg_chars:
                    errors.append(ValidationError(
                        "messages",
                        f"total content exceeds {self._max_msg_chars} chars"
                    ))
                    break
        return errors
```

---

## Solution 2: MessageRoleSanitiser — Strip Injected System Roles

```python
import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


class MessageRoleSanitiser:
    """
    Removes or demotes disallowed roles from the messages array.
    Specifically, prevents clients from injecting 'system' role messages
    that would override the server's system prompt.

    Usage:
        sanitiser = MessageRoleSanitiser()
        safe_messages = sanitiser.sanitise(client_messages)
    """

    ALLOWED_CLIENT_ROLES = frozenset({"user", "assistant"})

    def __init__(self, on_violation=None):
        self._callback = on_violation or self._log_violation

    @staticmethod
    def _log_violation(msg: Dict[str, Any], i: int):
        logger.warning(
            "message_role_injection_attempt index=%d role=%s",
            i, msg.get("role"),
        )

    def sanitise(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        safe = []
        for i, msg in enumerate(messages):
            role = msg.get("role", "")
            if role not in self.ALLOWED_CLIENT_ROLES:
                self._callback(msg, i)
                # Demote to user with a warning prefix instead of dropping
                safe.append({
                    "role": "user",
                    "content": f"[sanitised: role '{role}' not allowed] {msg.get('content', '')}",
                })
            else:
                safe.append(msg)
        return safe

    def has_injection_attempt(self, messages: List[Dict[str, Any]]) -> bool:
        return any(
            msg.get("role") not in self.ALLOWED_CLIENT_ROLES
            for msg in messages
        )
```

---

## Solution 3: RequestSizeLimiter — Block Oversized Payloads Before Parsing

```python
import json
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


class RequestSizeLimiter:
    """
    Validates raw request byte size and token estimates before
    JSON parsing or LLM invocation. Prevents payload-based DoS attacks
    that consume memory during JSON deserialization.

    Usage:
        limiter = RequestSizeLimiter(
            max_bytes=512_000,          # 512 KB raw request
            max_estimated_tokens=10_000,
        )
        limiter.check_bytes(raw_body)
        request_dict = json.loads(raw_body)
        limiter.check_estimated_tokens(request_dict)
    """

    CHARS_PER_TOKEN_ESTIMATE = 4.0

    def __init__(self, max_bytes: int = 512_000,
                  max_estimated_tokens: int = 10_000):
        self._max_bytes = max_bytes
        self._max_tokens = max_estimated_tokens

    def check_bytes(self, body: bytes):
        if len(body) > self._max_bytes:
            raise ValueError(
                f"Request body {len(body)} bytes exceeds limit {self._max_bytes}"
            )

    def check_estimated_tokens(self, request: dict):
        messages = request.get("messages", [])
        total_chars = sum(
            len(m.get("content", "") if isinstance(m.get("content"), str)
                else " ".join(
                    c.get("text", "") for c in m.get("content", [])
                    if isinstance(c, dict)
                ))
            for m in messages
        )
        system = request.get("system", "") or ""
        total_chars += len(system)
        estimated_tokens = total_chars / self.CHARS_PER_TOKEN_ESTIMATE
        if estimated_tokens > self._max_tokens:
            raise ValueError(
                f"Estimated {estimated_tokens:.0f} tokens exceeds limit {self._max_tokens}"
            )

    def check(self, body: bytes):
        self.check_bytes(body)
        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON: {exc}") from exc
        self.check_estimated_tokens(data)
        return data
```

---

## Solution 4: AllowlistParameterFilter — Remove Unknown Request Fields

```python
from typing import Any, Dict, Set


class AllowlistParameterFilter:
    """
    Strips any request parameters not on the allowlist before they reach
    the LLM SDK. Prevents parameter injection attacks where clients
    supply vendor-specific flags (e.g., tool_choice, functions, response_format)
    that the agent does not intend to expose.

    Usage:
        filt = AllowlistParameterFilter(
            allowed={"messages", "model", "max_tokens", "temperature", "stream"}
        )
        safe_params = filt.filter(raw_params)
        response = await client.invoke(**safe_params)
    """

    def __init__(self, allowed: Optional[Set[str]] = None):
        self._allowed = allowed or {
            "messages", "model", "max_tokens",
            "temperature", "system", "stream",
        }
        self._stripped_count = 0

    def filter(self, params: Dict[str, Any]) -> Dict[str, Any]:
        safe = {}
        stripped = []
        for k, v in params.items():
            if k in self._allowed:
                safe[k] = v
            else:
                stripped.append(k)
                self._stripped_count += 1
        if stripped:
            import logging
            logging.getLogger(__name__).warning(
                "request_params_stripped fields=%s", stripped
            )
        return safe

    def total_stripped(self) -> int:
        return self._stripped_count
```

---

## Solution 5: ValidationMiddleware — Composable Validation Chain

```python
import asyncio
import json
import logging
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)


class ValidationMiddleware:
    """
    Composes all validation steps into a single middleware callable.
    Reject at the earliest possible stage to minimise overhead.

    Usage:
        middleware = ValidationMiddleware(
            allowed_models={"claude-sonnet-4-6"},
            max_bytes=256_000,
            max_tokens=8_000,
        )

        @app.post("/v1/chat")
        async def chat(request: Request):
            result = middleware.process(await request.body())
            if not result["valid"]:
                return JSONResponse({"errors": result["errors"]}, status_code=422)
            safe_req = result["request"]
            return await llm_client.invoke(safe_req)
    """

    def __init__(self, allowed_models=None, max_bytes=512_000,
                  max_tokens=10_000, max_messages=100):
        self._limiter = RequestSizeLimiter(max_bytes, max_tokens)
        self._validator = LLMRequestValidator(
            allowed_models=allowed_models,
            max_messages=max_messages,
        )
        self._sanitiser = MessageRoleSanitiser()
        self._filter = AllowlistParameterFilter()

    def process(self, body: bytes) -> Dict[str, Any]:
        # Stage 1: size check (no JSON parsing yet)
        try:
            raw = self._limiter.check(body)
        except ValueError as exc:
            return {"valid": False, "errors": [str(exc)]}

        # Stage 2: role injection check
        messages = raw.get("messages", [])
        if self._sanitiser.has_injection_attempt(messages):
            logger.warning("role_injection_attempt detected")
            raw["messages"] = self._sanitiser.sanitise(messages)

        # Stage 3: allowlist filter
        raw = self._filter.filter(raw)

        # Stage 4: full schema validation
        result = self._validator.validate(raw)
        if not result.valid:
            return {
                "valid": False,
                "errors": [e.message for e in result.errors],
            }

        return {"valid": True, "request": result.sanitised}
```

---

## Solution 6: ValidationAuditLogger — Log Rejected Requests for Threat Analysis

```python
import hashlib
import json
import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class ValidationAuditLogger:
    """
    Records all validation rejections with anonymised details for
    threat analysis. Identifies patterns of parameter injection,
    role injection, or oversized payload attacks.

    Usage:
        audit = ValidationAuditLogger()
        audit.record_rejection(
            client_ip="192.168.1.1",
            errors=result.errors,
            raw_fields=list(raw_params.keys()),
        )
    """

    def __init__(self, log_path: Optional[str] = None):
        self._path = log_path
        self._counts: Dict[str, int] = {}

    def record_rejection(self, client_ip: str,
                          errors: List[Any],
                          raw_fields: Optional[List[str]] = None):
        ip_hash = hashlib.sha256(client_ip.encode()).hexdigest()[:12]
        error_types = [
            e.field if hasattr(e, "field") else str(e)
            for e in errors
        ]
        for t in error_types:
            self._counts[t] = self._counts.get(t, 0) + 1

        entry = {
            "ts": time.time(),
            "client_hash": ip_hash,
            "error_count": len(errors),
            "error_fields": error_types,
            "raw_fields": raw_fields or [],
        }
        logger.warning("validation_rejected entry=%s", json.dumps(entry))
        if self._path:
            with open(self._path, "a") as f:
                f.write(json.dumps(entry) + "\n")

    def threat_summary(self) -> Dict[str, int]:
        return dict(sorted(
            self._counts.items(), key=lambda x: -x[1]
        ))
```

---

## Comparison

| Approach | Schema Validation | Role Injection | Size Limit | Allowlist | Audit |
|---|---|---|---|---|---|
| **LLMRequestValidator** | Yes | Partial | No | No | No |
| **MessageRoleSanitiser** | No | Yes | No | No | No |
| **RequestSizeLimiter** | No | No | Yes | No | No |
| **AllowlistParameterFilter** | No | No | No | Yes | No |
| **ValidationMiddleware** | Yes | Yes | Yes | Yes | No |
| **ValidationAuditLogger** | No | No | No | No | Yes |

**Key insight**: always validate before deserialising — check raw body size before calling `json.loads`. After parsing, apply role sanitisation before anything else (role injection is the highest-impact attack). Then validate parameter types and ranges. Finally, apply the allowlist filter to strip any vendor-specific parameters the client should not control. The entire chain should run in under 1 ms for typical payloads and adds zero LLM latency.
