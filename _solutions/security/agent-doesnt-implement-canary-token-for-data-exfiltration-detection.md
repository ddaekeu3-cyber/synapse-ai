---
title: "Agent Doesn't Implement Canary Token for Data Exfiltration Detection"
description: "AI agents that access sensitive data have no mechanism to detect when that data is being exfiltrated through prompt injection or a compromised tool chain. Canary tokens are synthetic data records—fake API keys, bogus user records, honeypot credentials—embedded in datasets that should never appear in agent outputs. When a canary value surfaces in a response or is used externally, it triggers an immediate alert indicating unauthorized data access."
date: 2025-02-22
difficulty: advanced
category: security
slug: agent-doesnt-implement-canary-token-for-data-exfiltration-detection
tags:
  - canary-token
  - honeytoken
  - data-exfiltration
  - prompt-injection
  - security
  - detection
  - honeypot
symptoms:
  - "No way to know if a prompt injection caused the agent to leak database records to an attacker"
  - "Agent's tool outputs are not monitored for sensitive data patterns appearing in responses"
  - "Cannot detect if a compromised tool is forwarding context window contents externally"
  - "Security team has no tripwire for unauthorized access to the agent's knowledge base"
  - "Audit logs show tool calls but cannot determine if retrieved data was later exfiltrated"
---

## Problem

Agents operate over sensitive data—user records, internal documents, API credentials—and tool outputs are injected directly into the context window. A prompt injection attack or a compromised tool can cause the agent to repeat sensitive data in its responses or forward it to an attacker-controlled endpoint. Unlike network-level DLP, which scans traffic, canary tokens work by planting synthetic data in the dataset: a fake user with email `canary-7f3a@internal.corp`, a bogus API key `sk-canary-8b2d1c`, a honeypot document titled "Confidential Q4 Targets". If these values ever appear in agent outputs, external requests, or logs, the detection system fires immediately—indicating that data the agent should only read silently has been exfiltrated.

---

## Solution 1: CanaryTokenRegistry — Token Generation and Registration

```python
import hashlib
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class CanaryToken:
    token_id: str
    value: str
    category: str          # "api_key", "email", "user_record", "document"
    description: str = ""
    created_at: float = field(default_factory=time.time)
    triggered: bool = False
    trigger_count: int = 0


class CanaryTokenRegistry:
    """
    Generates and manages canary tokens for injection into datasets.
    Each token has a unique value and category. The registry exposes
    a scanner that checks agent outputs for any registered token value.

    Usage:
        registry = CanaryTokenRegistry(alert_fn=send_security_alert)
        api_key = registry.create("api_key", "Fake AWS key in config store")
        email = registry.create("email", "Honeypot user in user table")

        # Embed token.value into your dataset
        # In the agent response pipeline:
        registry.scan_output(agent_response, session_id="sess-001")
    """

    CATEGORY_FORMATS = {
        "api_key": "sk-canary-{hex}",
        "email": "canary-{hex}@honeypot.internal",
        "aws_key": "AKIA{upper}CANARY{upper2}",
        "jwt": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.canary-{hex}.signature",
        "url": "https://canary-{hex}.honeypot.internal/trap",
        "filename": "CONFIDENTIAL_canary_{hex}.xlsx",
        "ssn": "000-{d1}-{d2}",  # SSNs starting with 000 are invalid/detectable
    }

    def __init__(self, alert_fn: Optional[Callable] = None):
        self._tokens: Dict[str, CanaryToken] = {}
        self._alert = alert_fn

    def create(self, category: str, description: str = "") -> CanaryToken:
        hex_val = secrets.token_hex(8)
        fmt = self.CATEGORY_FORMATS.get(category, "CANARY-{hex}")
        value = fmt.format(
            hex=hex_val,
            upper=hex_val[:8].upper(),
            upper2=hex_val[8:].upper() if len(hex_val) >= 16 else hex_val[:4].upper(),
            d1=hex_val[:2],
            d2=hex_val[2:4],
        )
        token_id = hashlib.sha256(value.encode()).hexdigest()[:12]
        token = CanaryToken(
            token_id=token_id,
            value=value,
            category=category,
            description=description,
        )
        self._tokens[token_id] = token
        logger.info("canary_token_created id=%s category=%s", token_id, category)
        return token

    def scan_output(self, text: str, session_id: str = "", context: Dict = None) -> List[CanaryToken]:
        """Scan text for any registered canary token values. Returns triggered tokens."""
        triggered = []
        for token in self._tokens.values():
            if token.value in text:
                token.triggered = True
                token.trigger_count += 1
                triggered.append(token)
                logger.critical(
                    "CANARY_TOKEN_TRIGGERED id=%s category=%s session_id=%s value_prefix=%s",
                    token.token_id, token.category, session_id, token.value[:8] + "...",
                )
                if self._alert:
                    try:
                        self._alert({
                            "event": "canary_token_triggered",
                            "token_id": token.token_id,
                            "category": token.category,
                            "session_id": session_id,
                            "context": context or {},
                            "ts": time.time(),
                        })
                    except Exception as exc:
                        logger.error("canary_alert_failed error=%s", exc)
        return triggered

    def scan_request(self, url: str, headers: Dict, session_id: str = "") -> List[CanaryToken]:
        """Scan an outbound HTTP request for canary values in URL or headers."""
        combined = url + str(headers)
        return self.scan_output(combined, session_id=session_id,
                                  context={"type": "outbound_request", "url": url})

    def all_values(self) -> List[str]:
        return [t.value for t in self._tokens.values()]

    def triggered_tokens(self) -> List[CanaryToken]:
        return [t for t in self._tokens.values() if t.triggered]
```

---

## Solution 2: CanaryDocumentInjector — Plant Honeypot Records in Datasets

```python
import json
import logging
import random
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class CanaryDocumentInjector:
    """
    Injects synthetic honeypot documents into the agent's knowledge base
    or retrieved context. If the agent ever surfaces the contents of a
    canary document in its output, it indicates unauthorized retrieval
    (e.g., via an overly broad similarity search or a prompt injection
    that queries for all documents).

    Usage:
        injector = CanaryDocumentInjector(registry=canary_registry)
        canary_docs = injector.create_batch(count=5, category="financial")
        for doc in canary_docs:
            vector_store.upsert(doc["id"], doc["embedding"], doc["metadata"])
    """

    TEMPLATES = {
        "financial": {
            "title": "CONFIDENTIAL: Q{q} {year} Revenue Projections — DO NOT DISTRIBUTE",
            "body": (
                "Internal financial summary for Q{q} {year}. "
                "Projected revenue: ${revenue}M. Net margin: {margin}%. "
                "Authorized personnel only. Document ID: CANARY-FIN-{token}"
            ),
        },
        "credentials": {
            "title": "System Access Credentials — Privileged Access Management",
            "body": (
                "Database root password: canary-{token}-db\n"
                "Admin API key: sk-canary-{token}\n"
                "SSH private key fingerprint: SHA256:canary{token}\n"
                "This document is a security trap. Unauthorized access has been logged."
            ),
        },
        "user_record": {
            "title": "User Profile: Honeypot Account",
            "body": (
                "Name: Canarya Honeypot\nEmail: canary-{token}@trap.internal\n"
                "SSN: 000-{d1}-{d2}\nAccount: CANARY{token_upper}\n"
                "This is a synthetic record for security monitoring."
            ),
        },
    }

    def __init__(self, registry: CanaryTokenRegistry):
        self._registry = registry

    def create_document(self, category: str = "financial") -> Dict[str, Any]:
        token = self._registry.create(category, description=f"Canary document ({category})")
        template = self.TEMPLATES.get(category, self.TEMPLATES["financial"])
        import secrets
        hex8 = secrets.token_hex(4)
        body = template["body"].format(
            token=token.value[:12],
            token_upper=token.value[:8].upper(),
            q=random.randint(1, 4),
            year=2024 + random.randint(0, 1),
            revenue=random.randint(100, 999),
            margin=random.randint(10, 40),
            d1=hex8[:2],
            d2=hex8[2:4],
        )
        title = template["title"].format(
            q=random.randint(1, 4),
            year=2025,
        )
        return {
            "id": f"canary-doc-{token.token_id}",
            "title": title,
            "body": body,
            "metadata": {
                "canary": True,
                "token_id": token.token_id,
                "category": category,
                "created_at": time.time(),
            },
            "full_text": f"{title}\n\n{body}",
        }

    def create_batch(self, count: int = 5, category: str = "financial") -> List[Dict[str, Any]]:
        return [self.create_document(category) for _ in range(count)]
```

---

## Solution 3: OutputCanaryScanner — Middleware for Response Scanning

```python
import asyncio
import functools
import logging
import time
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)


class OutputCanaryScanner:
    """
    Wraps agent response generation with a post-processing scanner.
    Every response is scanned for canary token values before delivery.
    On trigger: logs the event, optionally blocks delivery, and fires
    the alert callback.

    Usage:
        scanner = OutputCanaryScanner(
            registry=canary_registry,
            block_on_trigger=True,   # replace response with a security notice
            alert_fn=page_security_team,
        )

        @scanner.wrap
        async def generate_response(messages, session_id):
            return await llm.complete(messages)
    """

    BLOCKED_RESPONSE = (
        "[SECURITY: This response was blocked because it contained "
        "content that triggered a security monitoring alert. "
        "This incident has been reported.]"
    )

    def __init__(
        self,
        registry: CanaryTokenRegistry,
        block_on_trigger: bool = True,
        alert_fn: Optional[Callable] = None,
    ):
        self._registry = registry
        self._block = block_on_trigger
        self._alert = alert_fn
        self._scans_total = 0
        self._triggers_total = 0

    def scan_and_maybe_block(
        self, response: str, session_id: str = "", context: Dict = None
    ) -> str:
        self._scans_total += 1
        triggered = self._registry.scan_output(response, session_id=session_id, context=context)
        if triggered:
            self._triggers_total += 1
            logger.critical(
                "output_scanner_triggered session=%s tokens=%s",
                session_id, [t.token_id for t in triggered],
            )
            if self._alert:
                try:
                    self._alert({"session_id": session_id, "triggered_tokens": len(triggered)})
                except Exception:
                    pass
            if self._block:
                return self.BLOCKED_RESPONSE
        return response

    def wrap(self, fn: Callable):
        """Decorator: scan every string return value for canary tokens."""
        @functools.wraps(fn)
        async def async_wrapper(*args, session_id: str = "", **kwargs):
            result = await fn(*args, session_id=session_id, **kwargs)
            if isinstance(result, str):
                result = self.scan_and_maybe_block(result, session_id=session_id)
            return result

        @functools.wraps(fn)
        def sync_wrapper(*args, session_id: str = "", **kwargs):
            result = fn(*args, session_id=session_id, **kwargs)
            if isinstance(result, str):
                result = self.scan_and_maybe_block(result, session_id=session_id)
            return result

        return async_wrapper if asyncio.iscoroutinefunction(fn) else sync_wrapper

    @property
    def stats(self) -> Dict:
        return {
            "scans_total": self._scans_total,
            "triggers_total": self._triggers_total,
            "trigger_rate_pct": round(
                self._triggers_total / max(self._scans_total, 1) * 100, 3
            ),
        }
```

---

## Solution 4: OutboundRequestCanaryMonitor — Detect Exfiltration via HTTP Calls

```python
import logging
import time
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


class OutboundRequestCanaryMonitor:
    """
    Wraps outbound HTTP tool calls and checks request URLs, headers, and
    bodies for canary token values. A canary value appearing in an outbound
    request indicates the agent was manipulated into forwarding sensitive
    data to an external endpoint.

    Usage:
        monitor = OutboundRequestCanaryMonitor(registry=canary_registry)
        # Before making any HTTP tool call:
        monitor.check_request(url, headers, body, session_id="sess-001")
    """

    # Domains that are allowed to receive canary-adjacent requests
    INTERNAL_DOMAINS = frozenset(["localhost", "127.0.0.1", "::1"])

    def __init__(
        self,
        registry: CanaryTokenRegistry,
        alert_fn: Optional[Callable] = None,
        block_suspicious: bool = True,
    ):
        self._registry = registry
        self._alert = alert_fn
        self._block = block_suspicious
        self._checks = 0
        self._blocked = 0

    def check_request(
        self,
        url: str,
        headers: Optional[Dict] = None,
        body: Optional[str] = None,
        session_id: str = "",
    ) -> bool:
        """Returns True if request is safe to proceed, False if blocked."""
        self._checks += 1
        combined = url + str(headers or {}) + (body or "")
        triggered = self._registry.scan_output(combined, session_id=session_id,
                                                context={"type": "outbound_http", "url": url})
        if not triggered:
            return True

        parsed = urlparse(url)
        is_internal = parsed.hostname in self.INTERNAL_DOMAINS

        self._blocked += 1
        logger.critical(
            "OUTBOUND_CANARY_DETECTED url=%s session=%s tokens=%d internal=%s",
            url, session_id, len(triggered), is_internal,
        )

        if self._alert:
            try:
                self._alert({
                    "event": "outbound_canary_detected",
                    "url": url,
                    "session_id": session_id,
                    "triggered_token_ids": [t.token_id for t in triggered],
                    "ts": time.time(),
                })
            except Exception:
                pass

        return not self._block  # if block_suspicious, return False to abort request

    @property
    def stats(self) -> Dict:
        return {"checks": self._checks, "blocked": self._blocked}
```

---

## Solution 5: DatabaseCanaryRowInjector — Honeypot Records in Production Tables

```python
import logging
import time
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


class DatabaseCanaryRowInjector:
    """
    Inserts synthetic honeypot rows into production database tables that
    the agent can read. Wraps the database query layer to detect when
    canary row values appear in query results returned to the agent,
    then scans those results before injecting them into the context.

    Usage:
        injector = DatabaseCanaryRowInjector(
            registry=canary_registry,
            db_client=async_db,
            scanner=output_scanner,
        )
        await injector.inject_users_table(count=3)
        # Later, intercept query results:
        rows = await injector.monitored_query("SELECT * FROM users WHERE ...", ...)
    """

    def __init__(
        self,
        registry: CanaryTokenRegistry,
        db_client: Any,
        scanner: OutputCanaryScanner,
    ):
        self._registry = registry
        self._db = db_client
        self._scanner = scanner
        self._canary_ids: List[str] = []

    async def inject_users_table(self, count: int = 3):
        for i in range(count):
            token = self._registry.create("email", f"Canary user row #{i}")
            row = {
                "email": token.value,
                "name": f"Canary Honeypot {i}",
                "created_at": time.time(),
                "is_canary": True,
                "_canary_token_id": token.token_id,
            }
            try:
                result = await self._db.execute(
                    "INSERT INTO users (email, name, created_at) VALUES ($1, $2, $3)",
                    token.value, row["name"], row["created_at"],
                )
                self._canary_ids.append(token.token_id)
                logger.info("canary_row_injected table=users token_id=%s", token.token_id)
            except Exception as exc:
                logger.error("canary_row_inject_failed error=%s", exc)

    async def monitored_query(self, sql: str, *params, session_id: str = "") -> List[Dict]:
        rows = await self._db.fetch(sql, *params)
        if rows:
            rows_str = str(rows)
            self._scanner.scan_and_maybe_block(rows_str, session_id=session_id,
                                                context={"type": "db_query", "sql": sql[:100]})
        return rows
```

---

## Solution 6: CanaryAlertDispatcher — Multi-Channel Security Incident Notification

```python
import json
import logging
import time
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


class CanaryAlertDispatcher:
    """
    Routes canary token trigger alerts to multiple notification channels:
    structured log (for SIEM), webhook (PagerDuty, Slack), and an in-memory
    incident ledger for dashboard queries. Deduplicates repeated alerts for
    the same token within a cooldown window.

    Usage:
        dispatcher = CanaryAlertDispatcher(
            webhook_url="https://events.pagerduty.com/v2/enqueue",
            cooldown_seconds=60,
        )
        registry = CanaryTokenRegistry(alert_fn=dispatcher.dispatch)
    """

    def __init__(
        self,
        webhook_url: Optional[str] = None,
        webhook_headers: Optional[Dict] = None,
        cooldown_seconds: float = 60.0,
        extra_handlers: Optional[List[Callable]] = None,
    ):
        self._webhook = webhook_url
        self._webhook_headers = webhook_headers or {"Content-Type": "application/json"}
        self._cooldown = cooldown_seconds
        self._extra = extra_handlers or []
        self._last_alert: Dict[str, float] = {}
        self._incidents: List[Dict[str, Any]] = []

    def _is_duplicate(self, token_id: str) -> bool:
        last = self._last_alert.get(token_id, 0.0)
        return time.time() - last < self._cooldown

    def dispatch(self, payload: Dict[str, Any]):
        token_id = payload.get("token_id", "unknown")
        if self._is_duplicate(token_id):
            logger.debug("canary_alert_deduplicated token_id=%s", token_id)
            return

        self._last_alert[token_id] = time.time()
        self._incidents.append({**payload, "dispatched_at": time.time()})

        # Structured SIEM log
        logger.critical("SECURITY_INCIDENT %s", json.dumps(payload))

        # Webhook (fire-and-forget; use background thread to not block agent)
        if self._webhook:
            import threading
            threading.Thread(
                target=self._send_webhook, args=(payload,), daemon=True
            ).start()

        # Extra handlers (e.g. Slack, email)
        for handler in self._extra:
            try:
                handler(payload)
            except Exception as exc:
                logger.error("canary_alert_handler_failed error=%s", exc)

    def _send_webhook(self, payload: Dict):
        import urllib.request
        try:
            req = urllib.request.Request(
                self._webhook,
                data=json.dumps(payload, default=str).encode(),
                headers=self._webhook_headers,
                method="POST",
            )
            urllib.request.urlopen(req, timeout=5)
            logger.info("canary_webhook_sent")
        except Exception as exc:
            logger.error("canary_webhook_failed error=%s", exc)

    @property
    def incidents(self) -> List[Dict]:
        return list(self._incidents)

    @property
    def total_incidents(self) -> int:
        return len(self._incidents)
```

---

## Comparison

| Approach | Token Generation | Output Scanning | Outbound Detection | DB Injection | Multi-Channel Alert | Deduplication |
|---|---|---|---|---|---|---|
| **CanaryTokenRegistry** | Yes | Yes | No | No | Via callback | No |
| **CanaryDocumentInjector** | Via registry | No | No | No | No | No |
| **OutputCanaryScanner** | No | Yes | No | No | No | No |
| **OutboundRequestCanaryMonitor** | No | No | Yes | No | No | No |
| **DatabaseCanaryRowInjector** | Via registry | Via scanner | No | Yes | No | No |
| **CanaryAlertDispatcher** | No | No | No | No | Yes | Yes |

**Key insight**: the minimum viable canary deployment is one planted fake credential (`CanaryTokenRegistry.create("api_key")`) whose value is added to the agent's system prompt or knowledge base, combined with `OutputCanaryScanner` wrapping the final response step. If the agent is ever manipulated into echoing the fake key in a response, the alert fires immediately. Add `CanaryDocumentInjector` to seed 5 honeypot documents into the vector store—these serve as tripwires for overly broad retrieval attacks. The key operational rule: canary token values must never appear in legitimate agent outputs under any normal usage, so any trigger is a true positive worth investigating.
