---
layout: solution
title: "Agent Doesn't Implement TLS Certificate Pinning for API Calls"
description: "How to prevent man-in-the-middle attacks on agent LLM API calls by pinning TLS certificates, validating certificate chains, and detecting unexpected certificate changes."
tags: [security, tls, certificates, network, api, mitm]
difficulty: advanced
solution_count: 6
---

## Problem

Agents call the Anthropic API and other LLM endpoints over HTTPS but rely entirely on the OS certificate store for validation. A compromised corporate proxy, a malicious network appliance, or a misconfigured TLS inspection device can silently intercept all API traffic — capturing system prompts, user data, and API keys — without the agent detecting anything unusual.

```python
# Bad: implicit trust in OS cert store — susceptible to MitM via custom CA
import httpx
client = httpx.AsyncClient()  # any cert trusted by OS is accepted
response = await client.post("https://api.anthropic.com/v1/messages", ...)
# A TLS-intercepting proxy with a custom CA can read all traffic
```

---

## Solution 1 — SPKI Fingerprint Pinning with httpx

Pin the Subject Public Key Info (SPKI) fingerprint of the Anthropic API certificate. Even if an attacker has a CA-signed certificate, their public key fingerprint won't match the pin.

```python
import hashlib
import ssl
import socket
import base64
import httpx
from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization

# Obtain current fingerprint (run once, store in config):
# python -c "
# from agent_tls import get_spki_fingerprint
# print(get_spki_fingerprint('api.anthropic.com', 443))
# "
ANTHROPIC_SPKI_PINS: set[str] = {
    # Add real fingerprints from Anthropic's certificate here
    # These are placeholders — fetch real pins before deploying
    "sha256/PLACEHOLDER_ANTHROPIC_PRIMARY_SPKI_PIN==",
    "sha256/PLACEHOLDER_ANTHROPIC_BACKUP_SPKI_PIN==",
}

def get_spki_fingerprint(host: str, port: int = 443) -> str:
    """Fetch the current SPKI fingerprint for a host."""
    ctx = ssl.create_default_context()
    with socket.create_connection((host, port), timeout=10) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as ssock:
            cert_der = ssock.getpeercert(binary_form=True)

    cert = x509.load_der_x509_certificate(cert_der, default_backend())
    spki_der = cert.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    digest = hashlib.sha256(spki_der).digest()
    return "sha256/" + base64.b64encode(digest).decode()

class PinnedTLSTransport(httpx.AsyncHTTPTransport):
    """httpx transport that validates SPKI fingerprint on every connection."""

    def __init__(self, pins: set[str], **kwargs):
        super().__init__(**kwargs)
        self._pins = pins

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        port = request.url.port or 443

        # Verify pin before sending any data
        try:
            actual_pin = get_spki_fingerprint(host, port)
        except Exception as e:
            raise ssl.SSLError(f"Certificate pin check failed (connection error): {e}")

        if actual_pin not in self._pins:
            raise ssl.SSLError(
                f"Certificate pin mismatch for {host}!\n"
                f"  Got:      {actual_pin}\n"
                f"  Expected: {self._pins}\n"
                f"Possible MitM attack — aborting request."
            )

        return await super().handle_async_request(request)

def make_pinned_client() -> httpx.AsyncClient:
    transport = PinnedTLSTransport(pins=ANTHROPIC_SPKI_PINS)
    return httpx.AsyncClient(transport=transport)

# Usage with Anthropic SDK (monkey-patch the httpx client)
import anthropic

async def create_pinned_anthropic_client() -> anthropic.AsyncAnthropic:
    # Anthropic SDK uses httpx internally
    pinned_http = make_pinned_client()
    return anthropic.AsyncAnthropic(
        http_client=pinned_http,
    )
```

---

## Solution 2 — Certificate Transparency Log Verification

Verify that the server's certificate appears in Certificate Transparency (CT) logs. Certificates issued without CT logging are a red flag for rogue CAs or private-CA MitM attacks.

```python
import ssl
import socket
import httpx
import json
from datetime import datetime
from cryptography import x509
from cryptography.hazmat.backends import default_backend

CT_LOG_ENDPOINT = "https://crt.sh/?q={}&output=json"

async def check_certificate_transparency(host: str) -> dict:
    """Verify the current certificate for a host appears in CT logs."""
    # Get the current certificate
    ctx = ssl.create_default_context()
    with socket.create_connection((host, 443), timeout=10) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as ssock:
            cert_der = ssock.getpeercert(binary_form=True)

    cert = x509.load_der_x509_certificate(cert_der, default_backend())
    serial_hex = format(cert.serial_number, "x")
    subject_cn = cert.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)[0].value
    not_after = cert.not_valid_after_utc

    # Query CT log
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(CT_LOG_ENDPOINT.format(subject_cn))

    if response.status_code != 200:
        return {"verified": False, "reason": "CT log query failed"}

    ct_entries = response.json()
    matching = [
        e for e in ct_entries
        if e.get("serial_number", "").lower().lstrip("0") == serial_hex.lower().lstrip("0")
    ]

    return {
        "verified": bool(matching),
        "host": host,
        "subject_cn": subject_cn,
        "serial_hex": serial_hex,
        "expires": not_after.isoformat(),
        "ct_log_entries": len(matching),
        "reason": "Found in CT logs" if matching else "NOT found in CT logs — possible rogue certificate",
    }

class CTVerifiedAnthropicClient:
    """Anthropic client that verifies CT log presence before first use."""

    def __init__(self):
        self._verified = False
        self._client = None

    async def _ensure_verified(self) -> None:
        if self._verified:
            return
        result = await check_certificate_transparency("api.anthropic.com")
        if not result["verified"]:
            raise ssl.SSLError(
                f"Anthropic API certificate NOT in CT logs: {result['reason']}\n"
                f"This may indicate a MitM attack or rogue CA. Refusing to connect."
            )
        self._verified = True
        print(f"CT verified: {result['ct_log_entries']} log entries for {result['host']}")

    async def messages_create(self, **kwargs) -> dict:
        await self._ensure_verified()
        import anthropic
        if self._client is None:
            self._client = anthropic.AsyncAnthropic()
        return await self._client.messages.create(**kwargs)

ct_client = CTVerifiedAnthropicClient()
```

---

## Solution 3 — Certificate Expiry Monitoring and Rotation Alerting

Continuously monitor the Anthropic API certificate's expiry date and alert when it's about to change (unexpected rotation can indicate a MitM swap).

```python
import asyncio
import ssl
import socket
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from cryptography import x509
from cryptography.hazmat.backends import default_backend

CERT_STATE_PATH = Path("/var/lib/agent/cert_state.json")
ALERT_DAYS_BEFORE_EXPIRY = 14
UNEXPECTED_CHANGE_ALERT_DAYS = 7  # alert if cert changes > 7 days before expiry

def fetch_cert_info(host: str, port: int = 443) -> dict:
    ctx = ssl.create_default_context()
    with socket.create_connection((host, port), timeout=10) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as ssock:
            cert_der = ssock.getpeercert(binary_form=True)

    cert = x509.load_der_x509_certificate(cert_der, default_backend())
    return {
        "serial": format(cert.serial_number, "x"),
        "subject": cert.subject.rfc4514_string(),
        "issuer": cert.issuer.rfc4514_string(),
        "not_before": cert.not_valid_before_utc.isoformat(),
        "not_after": cert.not_valid_after_utc.isoformat(),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }

def load_cert_state() -> dict | None:
    if CERT_STATE_PATH.exists():
        return json.loads(CERT_STATE_PATH.read_text())
    return None

def save_cert_state(info: dict) -> None:
    CERT_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CERT_STATE_PATH.write_text(json.dumps(info, indent=2))

def check_cert_change(previous: dict, current: dict) -> list[str]:
    alerts = []
    if previous["serial"] != current["serial"]:
        prev_expiry = datetime.fromisoformat(previous["not_after"])
        days_remaining = (prev_expiry - datetime.now(timezone.utc)).days
        if days_remaining > UNEXPECTED_CHANGE_ALERT_DAYS:
            alerts.append(
                f"UNEXPECTED cert rotation: serial changed from {previous['serial'][:8]}... "
                f"to {current['serial'][:8]}... with {days_remaining} days remaining on old cert"
            )

    expiry = datetime.fromisoformat(current["not_after"])
    days_to_expiry = (expiry - datetime.now(timezone.utc)).days
    if days_to_expiry < ALERT_DAYS_BEFORE_EXPIRY:
        alerts.append(
            f"Certificate expiring in {days_to_expiry} days: {current['subject']}"
        )
    return alerts

async def monitor_certificate(host: str, interval_hours: float = 1.0) -> None:
    while True:
        try:
            current = fetch_cert_info(host)
            previous = load_cert_state()

            if previous:
                alerts = check_cert_change(previous, current)
                for alert in alerts:
                    print(f"[CERT ALERT] {alert}")
                    # In production: send to PagerDuty, Slack, etc.

            save_cert_state(current)
            print(f"[cert-monitor] {host}: serial={current['serial'][:8]}... "
                  f"expires={current['not_after'][:10]}")
        except Exception as e:
            print(f"[cert-monitor] check failed: {e}")

        await asyncio.sleep(interval_hours * 3600)

# Run as a background task
asyncio.create_task(monitor_certificate("api.anthropic.com"))
```

---

## Solution 4 — Custom SSL Context with Restricted Cipher Suites

Restrict TLS negotiation to strong cipher suites and minimum TLS version, reducing the attack surface even if a MitM attacker attempts protocol downgrade.

```python
import ssl
import httpx
import anthropic

def create_hardened_ssl_context() -> ssl.SSLContext:
    """SSL context with strong settings and no weak ciphers."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3  # TLS 1.3 only
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.check_hostname = True
    ctx.load_default_certs()

    # Disable session tickets (forward secrecy hardening)
    ctx.options |= ssl.OP_NO_TICKET

    # Disable renegotiation
    ctx.options |= ssl.OP_NO_RENEGOTIATION

    # Disable compression (CRIME attack mitigation)
    ctx.options |= ssl.OP_NO_COMPRESSION

    # Restrict to AEAD cipher suites (ChaCha20-Poly1305, AES-GCM)
    # TLS 1.3 cipher suites are fixed; this applies to TLS 1.2 fallback
    try:
        ctx.set_ciphers(
            "ECDHE+AESGCM:ECDHE+CHACHA20:!aNULL:!eNULL:!LOW:!3DES:!RC4:!MD5"
        )
    except ssl.SSLError:
        pass  # TLS 1.3 only mode — cipher restriction handled by protocol

    return ctx

def make_hardened_anthropic_client() -> anthropic.AsyncAnthropic:
    ssl_ctx = create_hardened_ssl_context()
    httpx_client = httpx.AsyncClient(
        verify=ssl_ctx,
        timeout=httpx.Timeout(connect=5.0, read=120.0, write=30.0, pool=5.0),
    )
    return anthropic.AsyncAnthropic(http_client=httpx_client)

# Verify the configuration
def audit_ssl_context(ctx: ssl.SSLContext) -> dict:
    return {
        "minimum_tls_version": str(ctx.minimum_version),
        "verify_mode": str(ctx.verify_mode),
        "check_hostname": ctx.check_hostname,
        "options": {
            "no_ticket": bool(ctx.options & ssl.OP_NO_TICKET),
            "no_compression": bool(ctx.options & ssl.OP_NO_COMPRESSION),
        }
    }

client = make_hardened_anthropic_client()
audit = audit_ssl_context(create_hardened_ssl_context())
print(f"SSL audit: {audit}")
```

---

## Solution 5 — Network Egress Restriction: Whitelist API Endpoints

Prevent the agent from connecting to unexpected hosts by maintaining an allowlist of permitted TLS destinations and refusing all others at the transport layer.

```python
import httpx
import re
from dataclasses import dataclass

@dataclass
class EgressRule:
    host_pattern: str   # regex
    ports: set[int]
    description: str

EGRESS_ALLOWLIST: list[EgressRule] = [
    EgressRule(r"^api\.anthropic\.com$", {443}, "Anthropic API"),
    EgressRule(r"^api\.openai\.com$", {443}, "OpenAI API (if used)"),
    EgressRule(r"^.*\.googleapis\.com$", {443}, "Google APIs"),
    EgressRule(r"^localhost$", {8080, 8443}, "Local test server"),
]

class EgressFilteredTransport(httpx.AsyncHTTPTransport):
    def __init__(self, allowlist: list[EgressRule], **kwargs):
        super().__init__(**kwargs)
        self._allowlist = allowlist
        self._compiled = [
            (re.compile(rule.host_pattern), rule)
            for rule in allowlist
        ]

    def _is_allowed(self, host: str, port: int) -> tuple[bool, str]:
        for pattern, rule in self._compiled:
            if pattern.match(host) and port in rule.ports:
                return True, rule.description
        return False, ""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        port = request.url.port or (443 if request.url.scheme == "https" else 80)

        allowed, description = self._is_allowed(host, port)
        if not allowed:
            raise PermissionError(
                f"Egress blocked: {host}:{port} is not in the allowlist.\n"
                f"Allowed destinations: "
                f"{[r.description for r in self._allowlist]}"
            )

        import logging
        logging.getLogger("egress").info(
            f"Egress allowed: {host}:{port} ({description})"
        )
        return await super().handle_async_request(request)

def make_filtered_anthropic_client() -> anthropic.AsyncAnthropic:
    transport = EgressFilteredTransport(allowlist=EGRESS_ALLOWLIST)
    httpx_client = httpx.AsyncClient(transport=transport)
    return anthropic.AsyncAnthropic(http_client=httpx_client)

# Any attempt to connect to an unexpected host raises PermissionError
# This prevents prompt-injection attacks that try to exfiltrate data
# via unexpected tool calls to attacker-controlled servers
```

---

## Solution 6 — Automated Pin Refresh with Change Notification

Automate the process of updating SPKI pins when Anthropic rotates their certificate, with human notification and approval before the new pin goes live.

```python
import asyncio
import hashlib
import base64
import json
import ssl
import socket
import time
from pathlib import Path
from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from anthropic import AsyncAnthropic

PIN_STORE_PATH = Path("/etc/agent/tls_pins.json")

def compute_spki_pin(cert_der: bytes) -> str:
    cert = x509.load_der_x509_certificate(cert_der, default_backend())
    spki_der = cert.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    digest = hashlib.sha256(spki_der).digest()
    return "sha256/" + base64.b64encode(digest).decode()

def fetch_current_pins(host: str, port: int = 443) -> list[str]:
    """Fetch all certificates in the chain and compute pins for each."""
    ctx = ssl.create_default_context()
    pins = []
    with socket.create_connection((host, port), timeout=10) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as ssock:
            # Leaf certificate
            cert_der = ssock.getpeercert(binary_form=True)
            pins.append(compute_spki_pin(cert_der))
    return pins

def load_pin_store() -> dict:
    if PIN_STORE_PATH.exists():
        return json.loads(PIN_STORE_PATH.read_text())
    return {"pins": [], "updated_at": 0, "pending_pins": [], "approved": False}

def save_pin_store(store: dict) -> None:
    PIN_STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    PIN_STORE_PATH.write_text(json.dumps(store, indent=2))

async def notify_pin_change(old_pins: list[str], new_pins: list[str]) -> None:
    """Send notification about pin change — implement with your alerting system."""
    print(f"[TLS PIN CHANGE DETECTED]")
    print(f"  Old pins: {old_pins}")
    print(f"  New pins: {new_pins}")
    print("  Action required: review and approve new pins before agent can connect")
    # In production: POST to PagerDuty/Slack/email

async def pin_refresh_loop(host: str, interval_hours: float = 6.0) -> None:
    while True:
        try:
            current_pins = fetch_current_pins(host)
            store = load_pin_store()

            known_pins = set(store.get("pins", []))
            current_set = set(current_pins)

            if not known_pins:
                # First run — bootstrap
                store["pins"] = current_pins
                store["updated_at"] = time.time()
                store["approved"] = True
                save_pin_store(store)
                print(f"Bootstrapped pins for {host}: {current_pins}")
            elif current_set != known_pins:
                # Pin changed — require approval
                store["pending_pins"] = current_pins
                store["approved"] = False
                save_pin_store(store)
                await notify_pin_change(list(known_pins), current_pins)
            else:
                print(f"[pin-monitor] {host}: pins unchanged, approved={store['approved']}")

        except Exception as e:
            print(f"[pin-monitor] error: {e}")

        await asyncio.sleep(interval_hours * 3600)

def get_active_pins() -> set[str]:
    """Returns approved pins; falls back to empty set if unapproved change pending."""
    store = load_pin_store()
    if store.get("approved", False):
        return set(store.get("pins", []))
    return set()  # causes PinnedTLSTransport to block all connections until approved

# Approve pending pins (run by operator after verification):
def approve_pending_pins() -> None:
    store = load_pin_store()
    pending = store.get("pending_pins", [])
    if not pending:
        print("No pending pins to approve")
        return
    store["pins"] = pending
    store["pending_pins"] = []
    store["approved"] = True
    store["updated_at"] = time.time()
    save_pin_store(store)
    print(f"Approved new pins: {pending}")
```

---

## Comparison

| Approach | Prevents MitM | Detects Rogue CA | Auto-Recovers from Rotation | Ops Overhead | Best For |
|---|---|---|---|---|---|
| SPKI fingerprint pinning | **Yes** | **Yes** | No (manual update) | Low | High-security environments |
| CT log verification | **Yes** | **Yes** | **Yes** | Medium | Detecting private CA injection |
| Cert expiry monitoring | Partial | Partial | N/A | Low | Change alerting |
| Hardened SSL context | Partial | No | **Yes** | Low | Protocol downgrade prevention |
| Egress allowlist | **Yes** (by endpoint) | No | **Yes** | Low | Data exfiltration prevention |
| Automated pin refresh | **Yes** | **Yes** | With approval | High | Production with cert rotation |
