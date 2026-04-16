---
title: "Agent Doesn't Implement Structured JSON Logging for Machine-Parseable Output"
description: "AI agents that emit freeform text log lines require fragile regex parsing to extract fields like request_id, tool_name, and latency_ms in log aggregation systems. Structured JSON logging emits each log record as a parseable JSON object with consistent field names, enabling Elasticsearch, Splunk, and CloudWatch Logs Insights to filter, aggregate, and alert on individual fields without string parsing."
date: 2025-02-18
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-structured-json-logging-for-machine-parseable-output
tags:
  - structured-logging
  - json-logging
  - observability
  - log-aggregation
  - machine-parseable
  - elasticsearch
  - log-fields
symptoms:
  - "Log aggregation requires regex to extract latency from 'tool_call took 234ms'"
  - "No consistent request_id field across log lines from the same request"
  - "CloudWatch cannot filter by tool name because it is embedded in freeform text"
  - "Different modules emit different timestamp formats in their log messages"
  - "Alerting on error rate requires parsing unstructured strings instead of counting a field"
---

## Problem

Freeform log lines like `2025-02-18 INFO tool_call web_search took 234ms result_count=5` require brittle regex parsers to extract structured fields. When the format changes slightly, parsers break silently. Structured JSON logging emits each record as `{"ts": 1708300800, "level": "INFO", "tool": "web_search", "elapsed_ms": 234, "result_count": 5}` — every field is queryable directly in the log platform without parsing. Fields are named consistently across all modules, enabling cross-service correlation by `request_id`, aggregation by `tool`, and alerting on `error_count > threshold` as a simple numeric comparison.

---

## Solution 1: JSONFormatter — Python Logging Formatter for Structured Output

```python
import json
import logging
import time
import traceback
from typing import Any, Dict, Optional


class JSONFormatter(logging.Formatter):
    """
    Python logging Formatter that serializes each LogRecord to a
    single-line JSON object. Fields are consistently named according
    to the ECS (Elastic Common Schema) conventions where applicable.

    Usage:
        handler = logging.StreamHandler()
        handler.setFormatter(JSONFormatter(service="agent-api", version="1.2.0"))
        logging.getLogger().addHandler(handler)

        logger.info("tool_call_complete",
                     extra={"tool": "web_search", "elapsed_ms": 234})
        # Emits: {"ts":1708300800.123,"level":"INFO","msg":"tool_call_complete",
        #          "tool":"web_search","elapsed_ms":234,"service":"agent-api"}
    """

    # Fields from LogRecord that are included directly
    BASE_FIELDS = {
        "levelname": "level",
        "name": "logger",
        "filename": "file",
        "lineno": "line",
        "funcName": "func",
        "process": "pid",
    }

    # LogRecord fields to exclude from the 'extra' spillover
    RECORD_ATTRS = frozenset(
        vars(logging.LogRecord("", 0, "", 0, "", (), None)).keys()
        | {"message", "asctime", "exc_text"}
    )

    def __init__(self, service: str = "", version: str = "",
                  include_traceback: bool = True,
                  extra_fields: Optional[Dict[str, Any]] = None):
        super().__init__()
        self._service = service
        self._version = version
        self._include_tb = include_traceback
        self._extra = extra_fields or {}

    def format(self, record: logging.LogRecord) -> str:
        record.message = record.getMessage()

        doc: Dict[str, Any] = {
            "ts": record.created,
            "level": record.levelname,
            "msg": record.message,
        }

        # Standard record fields
        for attr, field_name in self.BASE_FIELDS.items():
            val = getattr(record, attr, None)
            if val is not None:
                doc[field_name] = val

        # Service metadata
        if self._service:
            doc["service"] = self._service
        if self._version:
            doc["version"] = self._version

        # Extra fields passed via extra= parameter
        for key, val in vars(record).items():
            if key not in self.RECORD_ATTRS and not key.startswith("_"):
                doc[key] = val

        # Static extra fields (tags, env)
        doc.update(self._extra)

        # Exception
        if record.exc_info and self._include_tb:
            doc["error"] = {
                "type": record.exc_info[0].__name__ if record.exc_info[0] else None,
                "message": str(record.exc_info[1]),
                "traceback": traceback.format_exception(*record.exc_info),
            }

        return json.dumps(doc, default=str, ensure_ascii=False)
```

---

## Solution 2: StructuredLogger — Contextual Field Binding

```python
import logging
import time
from contextlib import contextmanager
from typing import Any, Dict, Optional


class StructuredLogger:
    """
    Wraps Python's standard logger with contextual field binding.
    Fields bound to the logger are included in every subsequent log record
    without being passed explicitly to every call site.

    Usage:
        logger = StructuredLogger("agent.tool", service="agent-api")
        logger = logger.bind(request_id="abc123", user_id="u-456")

        logger.info("tool_call_start", tool="web_search", query="AI safety")
        logger.info("tool_call_end", tool="web_search", elapsed_ms=234)
        # Both records include request_id and user_id automatically.
    """

    def __init__(self, name: str, **bound_fields):
        self._logger = logging.getLogger(name)
        self._fields: Dict[str, Any] = bound_fields

    def bind(self, **fields) -> "StructuredLogger":
        """Return a new logger with additional bound fields."""
        new = StructuredLogger.__new__(StructuredLogger)
        new._logger = self._logger
        new._fields = {**self._fields, **fields}
        return new

    def _log(self, level: int, msg: str, **kwargs):
        extra = {**self._fields, **kwargs}
        self._logger.log(level, msg, extra=extra)

    def debug(self, msg: str, **kwargs):
        self._log(logging.DEBUG, msg, **kwargs)

    def info(self, msg: str, **kwargs):
        self._log(logging.INFO, msg, **kwargs)

    def warning(self, msg: str, **kwargs):
        self._log(logging.WARNING, msg, **kwargs)

    def error(self, msg: str, **kwargs):
        self._log(logging.ERROR, msg, **kwargs)

    def critical(self, msg: str, **kwargs):
        self._log(logging.CRITICAL, msg, **kwargs)

    def exception(self, msg: str, exc: Optional[Exception] = None, **kwargs):
        extra = {**self._fields, **kwargs}
        if exc:
            extra["error_type"] = type(exc).__name__
            extra["error_msg"] = str(exc)
        self._logger.exception(msg, extra=extra, exc_info=exc or True)

    @contextmanager
    def timed(self, operation: str, **kwargs):
        """Context manager that logs start/end with elapsed_ms."""
        t0 = time.monotonic()
        self.debug(f"{operation}_start", **kwargs)
        try:
            yield self
            elapsed = round((time.monotonic() - t0) * 1000, 1)
            self.info(f"{operation}_complete", elapsed_ms=elapsed, **kwargs)
        except Exception as exc:
            elapsed = round((time.monotonic() - t0) * 1000, 1)
            self.error(
                f"{operation}_failed",
                elapsed_ms=elapsed,
                error=str(exc),
                **kwargs,
            )
            raise
```

---

## Solution 3: LoggingConfiguration — Set Up JSON Logging at Startup

```python
import logging
import logging.config
import sys
from typing import Any, Dict, List, Optional


def configure_json_logging(
    service: str = "",
    version: str = "",
    level: str = "INFO",
    log_file: Optional[str] = None,
    extra_fields: Optional[Dict[str, Any]] = None,
):
    """
    Configure the Python root logger to emit JSON to stdout (and
    optionally to a file). Call once at agent startup.

    Usage:
        configure_json_logging(
            service="synapse-agent",
            version="2.1.0",
            level="INFO",
            extra_fields={"env": "production", "region": "us-east-1"},
        )
        logger = logging.getLogger("agent.tools")
        logger.info("Agent started")
    """
    formatter = JSONFormatter(
        service=service,
        version=version,
        extra_fields=extra_fields or {},
    )

    handlers: List[logging.Handler] = []

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(formatter)
    handlers.append(stdout_handler)

    if log_file:
        from logging.handlers import RotatingFileHandler
        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=50 * 1024 * 1024,   # 50 MB
            backupCount=5,
        )
        file_handler.setFormatter(formatter)
        handlers.append(file_handler)

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    # Remove existing handlers (avoid duplicate output)
    root.handlers.clear()
    for handler in handlers:
        root.addHandler(handler)

    logging.getLogger("agent").info(
        "logging_configured",
        extra={
            "service": service,
            "level": level,
            "handlers": len(handlers),
        },
    )
```

---

## Solution 4: RequestLogContext — Inject Request Fields into All Log Records

```python
import contextvars
import logging
import uuid
from typing import Any, Dict, Optional

_LOG_CONTEXT: contextvars.ContextVar[Dict[str, Any]] = contextvars.ContextVar(
    "log_context", default={}
)


class RequestLogContext:
    """
    Injects per-request fields (request_id, user_id, session_id) into
    every log record emitted within a request scope, without threading
    these values through every function signature.

    Usage:
        async with RequestLogContext.scope(user_id="u-123"):
            logger.info("processing_request")
            # Log record includes user_id="u-123", request_id="<auto>"
    """

    @staticmethod
    def from_contextmanager(**fields):
        """Set context fields and return the token for manual reset."""
        ctx = {
            "request_id": str(uuid.uuid4())[:8],
            **fields,
        }
        return _LOG_CONTEXT.set(ctx)

    @staticmethod
    def current() -> Dict[str, Any]:
        return _LOG_CONTEXT.get()

    @staticmethod
    def scope(**fields):
        """Async context manager that sets log context for the duration."""
        import contextlib

        @contextlib.asynccontextmanager
        async def _scope():
            token = _LOG_CONTEXT.set({
                "request_id": str(uuid.uuid4())[:8],
                **fields,
            })
            try:
                yield
            finally:
                _LOG_CONTEXT.reset(token)

        return _scope()


class ContextInjectingFilter(logging.Filter):
    """
    Logging Filter that injects ContextVar fields into every LogRecord.
    Add to handlers to make request context available in all log records.

    Usage:
        handler.addFilter(ContextInjectingFilter())
    """

    def filter(self, record: logging.LogRecord) -> bool:
        ctx = _LOG_CONTEXT.get()
        for key, val in ctx.items():
            if not hasattr(record, key):
                setattr(record, key, val)
        return True
```

---

## Solution 5: AgentAuditLog — Structured Audit Trail for Agent Actions

```python
import json
import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger("agent.audit")


class AgentAuditLog:
    """
    Emits structured audit events for security-relevant agent actions:
    tool invocations, context accesses, model calls, and errors.
    All events share a consistent schema queryable in SIEM tools.

    Usage:
        audit = AgentAuditLog(session_id="sess-001", agent_id="agent-A")
        audit.tool_call("web_search", params={"query": "AI"}, success=True, elapsed_ms=200)
        audit.llm_call(model="claude-sonnet-4-6", input_tokens=800, output_tokens=200)
        audit.error("validation_failed", tool="web_search", reason="timeout")
    """

    def __init__(self, session_id: str = "", agent_id: str = ""):
        self._session = session_id
        self._agent = agent_id
        self._seq = 0

    def _emit(self, event_type: str, **fields):
        self._seq += 1
        record = {
            "event": event_type,
            "session_id": self._session,
            "agent_id": self._agent,
            "seq": self._seq,
            "ts": time.time(),
            **fields,
        }
        logger.info(event_type, extra=record)

    def tool_call(self, tool: str,
                   params: Optional[Dict] = None,
                   success: bool = True,
                   elapsed_ms: float = 0.0,
                   result_size: int = 0):
        self._emit(
            "tool_call",
            tool=tool,
            param_keys=list((params or {}).keys()),
            success=success,
            elapsed_ms=round(elapsed_ms, 1),
            result_size=result_size,
        )

    def llm_call(self, model: str,
                  input_tokens: int = 0,
                  output_tokens: int = 0,
                  elapsed_ms: float = 0.0,
                  stop_reason: str = ""):
        self._emit(
            "llm_call",
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            elapsed_ms=round(elapsed_ms, 1),
            stop_reason=stop_reason,
        )

    def error(self, error_type: str, **context):
        self._emit("agent_error", error_type=error_type, **context)

    def session_end(self, total_turns: int, total_tokens: int):
        self._emit(
            "session_end",
            total_turns=total_turns,
            total_tokens=total_tokens,
            total_events=self._seq,
        )
```

---

## Solution 6: LogSchemaValidator — Enforce Consistent Field Names Across Modules

```python
import json
import logging
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


class LogSchemaValidator(logging.Handler):
    """
    Logging Handler that validates emitted JSON records against a schema
    of required and forbidden fields. Used in test/staging environments
    to catch modules that log inconsistently (missing request_id, wrong
    field names) before they reach production log pipelines.

    Usage:
        validator = LogSchemaValidator(
            required_fields={"ts", "level", "msg", "service"},
            forbidden_fields={"password", "secret", "token"},
        )
        logging.getLogger().addHandler(validator)
        # Any record missing required fields emits a schema_violation warning.
    """

    def __init__(self, required_fields: Optional[Set[str]] = None,
                  forbidden_fields: Optional[Set[str]] = None,
                  raise_on_violation: bool = False):
        super().__init__()
        self._required = required_fields or {"ts", "level", "msg"}
        self._forbidden = forbidden_fields or {"password", "secret", "api_key"}
        self._raise = raise_on_violation
        self._violations: List[Dict[str, Any]] = []

    def emit(self, record: logging.LogRecord):
        try:
            # Get the formatted JSON output
            formatted = self.format(record) if self.formatter else record.getMessage()
            try:
                doc = json.loads(formatted)
            except (json.JSONDecodeError, TypeError):
                return  # Not a JSON formatter in use

            missing = self._required - set(doc.keys())
            leaked = self._forbidden & set(doc.keys())

            if missing or leaked:
                violation = {
                    "logger": record.name,
                    "msg": record.getMessage()[:80],
                    "missing_fields": list(missing),
                    "forbidden_fields": list(leaked),
                }
                self._violations.append(violation)
                warning_msg = (
                    f"log_schema_violation logger={record.name} "
                    f"missing={missing} forbidden={leaked}"
                )
                if self._raise:
                    raise ValueError(warning_msg)
                # Use print to avoid recursive logging
                print(f"[LOG SCHEMA] {warning_msg}")
        except Exception:
            self.handleError(record)

    def violations(self) -> List[Dict[str, Any]]:
        return list(self._violations)

    def summary(self) -> Dict[str, Any]:
        return {
            "total_violations": len(self._violations),
            "required_fields": sorted(self._required),
            "forbidden_fields": sorted(self._forbidden),
        }
```

---

## Comparison

| Approach | JSON Output | Field Binding | Request Context | Audit Events | Schema Validation | Integrated |
|---|---|---|---|---|---|---|
| **JSONFormatter** | Yes | No | No | No | No | No |
| **StructuredLogger** | Via formatter | Yes | No | No | No | No |
| **LoggingConfiguration** | Yes | No | No | No | No | No |
| **RequestLogContext** | No | No | Yes | No | No | No |
| **AgentAuditLog** | Yes | No | No | Yes | No | No |
| **LogSchemaValidator** | No | No | No | No | Yes | No |

**Key insight**: the most impactful change is switching the root handler's formatter to `JSONFormatter` — every existing `logger.info(...)` call immediately starts emitting structured JSON without any other code changes. Add `ContextInjectingFilter` to the handler to get `request_id` in every record for free. In log aggregation systems (Elasticsearch, Splunk, CloudWatch), create index patterns on `tool`, `elapsed_ms`, `error_type`, and `session_id` — these four fields alone enable 90% of operational queries: "show all failed tool calls in the last hour", "average latency by tool", "sessions with errors today".
