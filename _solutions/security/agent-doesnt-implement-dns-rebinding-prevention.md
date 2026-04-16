---
title: "Agent Doesn't Implement DNS Rebinding Prevention"
description: "Locally-hosted AI agents that bind to localhost or a private network address are vulnerable to DNS rebinding attacks, allowing malicious websites to make cross-origin requests to the agent and exfiltrate conversation history or trigger tool execution."
category: security
difficulty: advanced
tags: [dns-rebinding, localhost, security, cors, host-validation, private-network, csrf]
---

# Agent Doesn't Implement DNS Rebinding Prevention

## Problem

A DNS rebinding attack lets an attacker's website bypass the Same-Origin Policy (SOP) against a locally-running agent. The attack flow: (1) attacker registers `evil.example.com` with a very short TTL, (2) browser visits the page, (3) attacker changes the DNS record to resolve to `127.0.0.1`, (4) subsequent requests from the same origin now target `localhost:8080` — the agent's HTTP API — with full cross-origin access. The agent sees requests from `evil.example.com` arriving on its local socket and, if it doesn't validate the `Host` header, treats them as legitimate. Result: the attacker can read conversation history, inject messages, and trigger tool calls.

## Solution 1: Host Header Allowlist Validation

The primary defense: reject any request whose `Host` header is not in an explicit allowlist of known-good hostnames.

```python
import asyncio
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

# Only these Host header values are accepted.
# Never include attacker-controlled hostnames.
ALLOWED_HOSTS = frozenset({
    "localhost",
    "127.0.0.1",
    "[::1]",
    "localhost:8080",
    "127.0.0.1:8080",
    "[::1]:8080",
})

def validate_host(host_header: str | None) -> bool:
    """
    Return True only if the Host header is in the allowlist.
    A missing, empty, or unexpected Host header is rejected.
    DNS rebinding attacks rely on a Host value like 'evil.example.com'
    that resolves to 127.0.0.1 — this check stops them cold.
    """
    if not host_header:
        return False
    # Strip port for comparison if not already included
    host = host_header.lower().strip()
    return host in ALLOWED_HOSTS

class AgentHandler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass

    def _reject(self, code: int, reason: str) -> None:
        body = reason.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        host = self.headers.get("Host", "")
        if not validate_host(host):
            self._reject(400, f"Rejected: Host header '{host}' not in allowlist")
            return

        if self.path == "/chat":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            import json
            data = json.loads(body)
            # Process agent request synchronously for demo
            import asyncio
            loop = asyncio.new_event_loop()
            resp = loop.run_until_complete(
                client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=128,
                    messages=[{"role": "user", "content": data.get("message", "")}],
                )
            )
            loop.close()
            response_body = json.dumps({"reply": resp.content[0].text}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", len(response_body))
            self.end_headers()
            self.wfile.write(response_body)
        else:
            self._reject(404, "Not found")

def run_secure_agent_server(port: int = 8080):
    server = HTTPServer(("127.0.0.1", port), AgentHandler)
    print(f"Agent server on 127.0.0.1:{port} (Host header validation active)")
    server.serve_forever()
```

**When to use**: Every locally-hosted agent HTTP server. Host header validation is the single most effective DNS rebinding countermeasure and adds zero latency.

---

## Solution 2: Origin + Referer Validation for Browser Clients

For agents accessed from a browser UI, validate both `Origin` and `Referer` headers to reject cross-origin requests.

```python
import re
from typing import Optional

# Allowed origins for browser-initiated requests
ALLOWED_ORIGINS = frozenset({
    "http://localhost:3000",      # dev UI
    "http://127.0.0.1:3000",
    "https://agent.mycompany.com", # production UI
})

def parse_origin(origin: str) -> tuple[str, str, Optional[int]]:
    """Parse origin into (scheme, host, port)."""
    try:
        parsed = __import__("urllib.parse", fromlist=["urlparse"]).urlparse(origin)
        return parsed.scheme, parsed.hostname or "", parsed.port
    except Exception:
        return "", "", None

def validate_origin(origin: Optional[str], referer: Optional[str]) -> bool:
    """
    Accept request only if Origin or Referer is in the allowlist.
    For same-origin requests (non-CORS), both may be absent — allow those.
    For cross-origin requests, at least one must be present and valid.
    """
    if origin is None and referer is None:
        # No CORS headers: likely a same-origin request or curl — allow
        return True

    if origin is not None:
        return origin.rstrip("/") in ALLOWED_ORIGINS

    # No Origin but Referer present: validate Referer prefix
    if referer is not None:
        for allowed in ALLOWED_ORIGINS:
            if referer.startswith(allowed):
                return True
        return False

    return False

def validate_request_headers(headers: dict) -> tuple[bool, str]:
    """
    Comprehensive header validation for DNS rebinding prevention.
    Returns (allowed, reason).
    """
    host = headers.get("Host", "")
    origin = headers.get("Origin")
    referer = headers.get("Referer")
    content_type = headers.get("Content-Type", "")

    # 1. Host header must be in allowlist
    allowed_hosts = {"localhost", "127.0.0.1", "localhost:8080", "127.0.0.1:8080"}
    host_bare = host.split(":")[0].lower()
    if host_bare not in {h.split(":")[0] for h in allowed_hosts}:
        return False, f"Host '{host}' not allowed"

    # 2. Origin/Referer must be valid if present
    if not validate_origin(origin, referer):
        return False, f"Origin '{origin}' or Referer '{referer}' not in allowlist"

    # 3. Content-Type must be application/json (not text/plain which bypasses CORS preflight)
    if content_type and "application/json" not in content_type:
        return False, f"Content-Type '{content_type}' rejected (must be application/json)"

    return True, "ok"

# FastAPI / Starlette middleware example
async def dns_rebinding_middleware(request, call_next):
    """ASGI middleware that validates headers on every request."""
    headers = dict(request.headers)
    allowed, reason = validate_request_headers(headers)
    if not allowed:
        from starlette.responses import Response
        return Response(f"Forbidden: {reason}", status_code=403)
    return await call_next(request)
```

**When to use**: Agents with a web UI. Origin + Content-Type validation blocks the text/plain CORS bypass that attackers use to avoid preflight OPTIONS checks.

---

## Solution 3: Private Network Access Header Enforcement

Chrome's Private Network Access (PNA) spec requires servers to respond to a preflight with `Access-Control-Allow-Private-Network: true` — but only after validating the request is legitimate. Implement PNA-aware CORS handling.

```python
import asyncio
from typing import Optional

CORS_ALLOWED_ORIGINS = {
    "http://localhost:3000",
    "http://127.0.0.1:3000",
}

def build_cors_headers(
    origin: Optional[str],
    request_private_network: bool = False,
) -> dict[str, str]:
    """
    Build CORS response headers.
    For PNA (Private Network Access) preflights, only grant permission
    to explicitly allowed origins — never wildcard.
    """
    headers: dict[str, str] = {}

    if origin in CORS_ALLOWED_ORIGINS:
        headers["Access-Control-Allow-Origin"] = origin
        headers["Vary"] = "Origin"
        headers["Access-Control-Allow-Credentials"] = "true"
        headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        headers["Access-Control-Allow-Headers"] = "Content-Type, X-Request-ID"
        headers["Access-Control-Max-Age"] = "86400"

        if request_private_network:
            # Only grant PNA access to known-good origins
            headers["Access-Control-Allow-Private-Network"] = "true"

    elif origin is not None:
        # Origin present but not allowed: explicitly deny
        # Do NOT echo back the origin — that would grant access
        pass

    return headers

class PNAAwareHandler:
    """
    Request handler that implements Private Network Access protection.
    Rejects cross-origin private network requests from untrusted origins.
    """

    def handle_preflight(self, origin: str, headers: dict) -> tuple[int, dict]:
        """Handle OPTIONS preflight. Returns (status_code, response_headers)."""
        if origin not in CORS_ALLOWED_ORIGINS:
            return 403, {"Content-Type": "text/plain"}

        request_pna = headers.get("Access-Control-Request-Private-Network") == "true"
        cors_headers = build_cors_headers(origin, request_private_network=request_pna)
        return 204, cors_headers

    def handle_request(self, origin: Optional[str], host: str) -> tuple[bool, str]:
        """Validate a non-preflight request."""
        # PNA browsers send Origin on private network requests
        if origin is not None and origin not in CORS_ALLOWED_ORIGINS:
            return False, f"Cross-origin request from '{origin}' denied"

        # Host must be localhost or 127.x
        host_bare = host.split(":")[0].lower()
        if host_bare not in {"localhost", "127.0.0.1", "::1"}:
            return False, f"Host '{host}' is not a local address"

        return True, "ok"

handler = PNAAwareHandler()

# Usage in an aiohttp / Starlette handler
async def agent_endpoint(request_headers: dict) -> dict:
    origin = request_headers.get("Origin")
    host = request_headers.get("Host", "")

    allowed, reason = handler.handle_request(origin, host)
    if not allowed:
        return {"error": "forbidden", "reason": reason}

    from anthropic import AsyncAnthropic
    client = AsyncAnthropic()
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content": "Hello"}],
    )
    return {"reply": resp.content[0].text}
```

**When to use**: Agents that will be accessed from Chrome 94+ browsers. PNA is Chrome's built-in DNS rebinding mitigation — implementing it server-side makes your agent compliant with the spec and protected by a second layer.

---

## Solution 4: Request Authentication Token — Make Unauthenticated Requests Unactionable

Require a secret token in every request header. The token is generated at agent startup and displayed to the user — it cannot be guessed by an attacker's JavaScript.

```python
import asyncio
import hashlib
import hmac
import secrets
import time
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

class AgentAccessToken:
    """
    Single-use-per-session shared secret between the agent process and its UI.
    Generated at startup; stored in memory only. Not persisted to disk.
    JavaScript on evil.example.com cannot read this value (SOP enforced by browser).
    """

    def __init__(self):
        self._token = secrets.token_urlsafe(32)
        self._issued_at = time.time()
        print(f"\n[agent] Access token: {self._token}\n")
        print("Set this token in your agent UI configuration.")

    def validate(self, provided: str | None) -> bool:
        if not provided:
            return False
        return hmac.compare_digest(self._token, provided)

    def make_csrf_token(self, session_id: str) -> str:
        """Derive a per-session CSRF token from the access token."""
        raw = f"{self._token}:{session_id}:{int(time.time() // 300)}"
        return hashlib.sha256(raw.encode()).hexdigest()

    def validate_csrf(self, session_id: str, csrf_token: str) -> bool:
        expected_now = self.make_csrf_token(session_id)
        # Allow previous 5-minute window (clock skew / tab persistence)
        prev_raw = f"{self._token}:{session_id}:{int(time.time() // 300) - 1}"
        expected_prev = hashlib.sha256(prev_raw.encode()).hexdigest()
        return (
            hmac.compare_digest(csrf_token, expected_now) or
            hmac.compare_digest(csrf_token, expected_prev)
        )

ACCESS_TOKEN = AgentAccessToken()

async def protected_agent_endpoint(
    x_agent_token: str | None,
    x_csrf_token: str | None,
    session_id: str,
    user_message: str,
) -> dict:
    """
    Endpoint that requires both the access token and a CSRF token.
    An attacker's JavaScript cannot obtain either value across origins.
    """
    if not ACCESS_TOKEN.validate(x_agent_token):
        return {"error": "unauthorized", "detail": "Missing or invalid X-Agent-Token"}

    if not ACCESS_TOKEN.validate_csrf(session_id, x_csrf_token or ""):
        return {"error": "forbidden", "detail": "CSRF token invalid"}

    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": user_message}],
    )
    return {"reply": resp.content[0].text}
```

**When to use**: Locally-running desktop agent apps. A startup-generated secret token that is never stored on disk is completely immune to DNS rebinding — the attacker's page cannot read `X-Agent-Token` from a different origin.

---

## Solution 5: Bind to 127.0.0.1 Only — Limit Attack Surface

Never bind to `0.0.0.0`. A server listening on all interfaces is reachable from any network interface, including those exposed to the LAN where other machines could initiate attacks.

```python
import asyncio
import socket
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

def create_loopback_socket(port: int) -> socket.socket:
    """
    Create a socket bound strictly to 127.0.0.1.
    This prevents the agent from being reachable from any external interface,
    which eliminates the network-level attack vector for DNS rebinding
    (attackers on the LAN cannot reach the agent at all).
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    # CRITICAL: bind to loopback only, not 0.0.0.0
    sock.bind(("127.0.0.1", port))
    sock.listen(128)
    sock.setblocking(False)
    return sock

async def handle_connection(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Minimal HTTP handler for demonstration."""
    peer = writer.get_extra_info("peername")
    peer_ip = peer[0] if peer else "unknown"

    # Extra check: reject if somehow called from non-loopback
    if peer_ip not in ("127.0.0.1", "::1"):
        writer.close()
        await writer.wait_closed()
        return

    raw = await reader.read(4096)
    # Parse Host header
    host_line = next((l for l in raw.decode(errors="ignore").split("\r\n")
                      if l.lower().startswith("host:")), "")
    host = host_line.split(":", 1)[1].strip() if ":" in host_line else ""

    allowed_hosts = {"localhost", "localhost:8080", "127.0.0.1", "127.0.0.1:8080"}
    if host.lower() not in allowed_hosts:
        response = b"HTTP/1.1 400 Bad Request\r\nContent-Length: 20\r\n\r\nInvalid Host header."
    else:
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=32,
            messages=[{"role": "user", "content": "ping"}],
        )
        body = resp.content[0].text.encode()
        response = (
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/plain\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
        )

    writer.write(response)
    await writer.drain()
    writer.close()
    await writer.wait_closed()

async def run_loopback_server(port: int = 8080):
    sock = create_loopback_socket(port)
    server = await asyncio.start_server(handle_connection, sock=sock)
    print(f"Agent bound to 127.0.0.1:{port} (loopback only)")
    async with server:
        await server.serve_forever()
```

**When to use**: All locally-hosted agents. Binding to `127.0.0.1` is the lowest-level defense — it physically restricts which network paths can reach the agent before any HTTP-level validation runs.

---

## Solution 6: HTTPS with Localhost Certificate — Prevent Protocol Downgrade

Serve the agent over HTTPS even on localhost. Browsers apply stricter CORS enforcement to HTTPS origins, and a valid certificate prevents MITM interception even on localhost.

```python
import asyncio
import ssl
import tempfile
import os
from pathlib import Path

def generate_self_signed_cert(hostname: str = "localhost") -> tuple[str, str]:
    """
    Generate a self-signed certificate for localhost.
    In production, use mkcert (https://github.com/FiloSottile/mkcert) to create
    a locally-trusted certificate instead.
    Returns (cert_path, key_path).
    """
    try:
        from cryptography import x509
        from cryptography.x509.oid import NameOID
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        import datetime

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.datetime.utcnow())
            .not_valid_after(datetime.datetime.utcnow() + datetime.timedelta(days=365))
            .add_extension(
                x509.SubjectAlternativeName([
                    x509.DNSName("localhost"),
                    x509.IPAddress(__import__("ipaddress").ip_address("127.0.0.1")),
                ]),
                critical=False,
            )
            .sign(key, hashes.SHA256())
        )

        tmp = tempfile.mkdtemp()
        cert_path = os.path.join(tmp, "cert.pem")
        key_path = os.path.join(tmp, "key.pem")

        Path(cert_path).write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        Path(key_path).write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            )
        )
        return cert_path, key_path
    except ImportError:
        raise RuntimeError("pip install cryptography to generate certs")

def create_tls_context(cert_path: str, key_path: str) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert_path, key_path)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    # Disable weak ciphers
    ctx.set_ciphers("ECDH+AESGCM:ECDH+CHACHA20:!aNULL:!MD5:!DSS")
    return ctx

async def run_https_agent(port: int = 8443):
    cert_path, key_path = generate_self_signed_cert("localhost")
    ssl_ctx = create_tls_context(cert_path, key_path)

    async def handler(reader, writer):
        peer_ip = writer.get_extra_info("peername", ["unknown"])[0]
        if peer_ip not in ("127.0.0.1", "::1"):
            writer.close()
            return
        # ... handle request ...
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handler, "127.0.0.1", port, ssl=ssl_ctx)
    print(f"HTTPS agent on https://localhost:{port}")
    print("Add cert to trust store or use mkcert for browser-trusted certs.")
    async with server:
        await server.serve_forever()
```

**When to use**: Desktop agent apps with a browser UI. HTTPS on localhost enables Secure cookies, strict CORS enforcement, and protects against MITM on shared machines.

---

## Comparison

| Solution | Blocks DNS Rebinding | Blocks LAN Attack | Browser Compat | Overhead | Best For |
|---|---|---|---|---|---|
| Host header allowlist | Yes | Partial | All | Zero | Every agent (mandatory baseline) |
| Origin + Referer validation | Yes | Yes | All | Zero | Agents with browser UIs |
| PNA header enforcement | Yes (Chrome) | Yes | Chrome 94+ | Zero | Chrome-targeted agents |
| Access token | Yes | Yes | All | ~0% | Desktop apps |
| Loopback-only binding | Yes | Yes | All | Zero | Network-level isolation |
| HTTPS on localhost | Partial | Partial | All (with trust) | TLS overhead | Browser UI with Secure cookies |

**Rule of thumb**: Always implement Host header validation (Solution 1) and bind to `127.0.0.1` only (Solution 5). These two controls together block all known DNS rebinding vectors with zero latency cost.
