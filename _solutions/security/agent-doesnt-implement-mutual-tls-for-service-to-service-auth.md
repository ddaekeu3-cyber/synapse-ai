---
title: "Agent Doesn't Implement Mutual TLS for Service-to-Service Auth"
description: "Without mTLS, inter-agent communication relies on bearer tokens or network-level trust alone, leaving it vulnerable to impersonation, man-in-the-middle attacks, and lateral movement if a token is compromised."
difficulty: advanced
category: security
tags: [mtls, tls, certificates, x509, spiffe, authentication, inter-agent, security]
---

## Problem

Agents calling each other over HTTP use one-way TLS (server certificate only) combined with a shared API key or bearer token. If a token is leaked or an internal network is compromised, any process can impersonate a legitimate agent. Mutual TLS eliminates this by requiring both sides to present a valid certificate signed by a trusted Certificate Authority, binding identity to cryptographic keys rather than secrets.

```python
# Broken: only server cert is verified; no client identity proof
import httpx

async def call_agent(url: str, payload: dict, token: str):
    async with httpx.AsyncClient(verify=True) as client:
        # Token in header — if stolen, anyone can impersonate this agent
        response = await client.post(url, json=payload,
                                     headers={"Authorization": f"Bearer {token}"})
    return response.json()
```

---

## Solution 1: Basic mTLS with ssl.SSLContext

```python
import ssl
import httpx
import asyncio
from pathlib import Path

def build_client_ssl_context(
    ca_cert: str,      # path to CA certificate (PEM)
    client_cert: str,  # path to this agent's certificate (PEM)
    client_key: str,   # path to this agent's private key (PEM)
) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.check_hostname = True
    ctx.load_verify_locations(ca_cert)          # trust only this CA
    ctx.load_cert_chain(client_cert, client_key)  # present our identity
    # Enforce TLS 1.3 minimum
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    return ctx

def build_server_ssl_context(
    ca_cert: str,
    server_cert: str,
    server_key: str,
) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.verify_mode = ssl.CERT_REQUIRED          # require client cert
    ctx.load_verify_locations(ca_cert)
    ctx.load_cert_chain(server_cert, server_key)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    return ctx

async def mtls_post(url: str, payload: dict,
                    ca_cert: str, client_cert: str, client_key: str) -> dict:
    ssl_ctx = build_client_ssl_context(ca_cert, client_cert, client_key)
    async with httpx.AsyncClient(verify=ssl_ctx) as client:
        response = await client.post(url, json=payload, timeout=10.0)
        response.raise_for_status()
        return response.json()

# Server-side: extract and log the client's identity from the certificate
from aiohttp import web

async def handle_agent_request(request: web.Request) -> web.Response:
    # aiohttp exposes the peer cert via the transport
    transport = request.transport
    if transport is None:
        raise web.HTTPForbidden(reason="No transport")
    peercert = transport.get_extra_info("peercert")
    if peercert is None:
        raise web.HTTPForbidden(reason="No client certificate")

    # Extract CN (Common Name) as agent identity
    subject = dict(x[0] for x in peercert.get("subject", []))
    agent_id = subject.get("commonName", "unknown")
    print(f"[mTLS] Request from agent: {agent_id}")

    return web.json_response({"status": "ok", "caller": agent_id})
```

---

## Solution 2: Self-Signed CA and Per-Agent Certificate Generation

```python
import subprocess
import tempfile
import os
from pathlib import Path
from dataclasses import dataclass

@dataclass
class AgentCertBundle:
    ca_cert_path: str
    agent_cert_path: str
    agent_key_path: str
    agent_id: str

class LocalCertificateAuthority:
    """
    In-process CA for development / single-cluster deployments.
    In production, use HashiCorp Vault PKI or cert-manager.
    """

    def __init__(self, ca_dir: str = "/tmp/agent-ca"):
        self.ca_dir = Path(ca_dir)
        self.ca_dir.mkdir(parents=True, exist_ok=True)
        self.ca_key = str(self.ca_dir / "ca.key")
        self.ca_cert = str(self.ca_dir / "ca.crt")

    def initialize(self, ca_common_name: str = "AgentCA"):
        """Generate CA key and self-signed certificate."""
        # Generate CA key
        subprocess.run([
            "openssl", "genrsa", "-out", self.ca_key, "4096"
        ], check=True, capture_output=True)
        # Generate CA certificate (valid 10 years for internal CA)
        subprocess.run([
            "openssl", "req", "-new", "-x509", "-days", "3650",
            "-key", self.ca_key, "-out", self.ca_cert,
            "-subj", f"/CN={ca_common_name}/O=AgentMesh"
        ], check=True, capture_output=True)

    def issue_agent_cert(self, agent_id: str,
                         validity_days: int = 90) -> AgentCertBundle:
        """Issue a certificate for a specific agent identity."""
        agent_dir = self.ca_dir / agent_id
        agent_dir.mkdir(exist_ok=True)
        key_path = str(agent_dir / "agent.key")
        csr_path = str(agent_dir / "agent.csr")
        cert_path = str(agent_dir / "agent.crt")
        ext_path = str(agent_dir / "ext.cnf")

        # Generate agent key
        subprocess.run(["openssl", "genrsa", "-out", key_path, "2048"],
                       check=True, capture_output=True)

        # Generate CSR
        subprocess.run([
            "openssl", "req", "-new", "-key", key_path, "-out", csr_path,
            "-subj", f"/CN={agent_id}/O=AgentMesh/OU=Agents"
        ], check=True, capture_output=True)

        # Extensions: client auth EKU + SAN
        with open(ext_path, "w") as f:
            f.write(f"""[ext]
extendedKeyUsage = clientAuth, serverAuth
subjectAltName = DNS:{agent_id}, DNS:{agent_id}.agents.local
""")

        # Sign with CA
        subprocess.run([
            "openssl", "x509", "-req", "-days", str(validity_days),
            "-in", csr_path, "-CA", self.ca_cert, "-CAkey", self.ca_key,
            "-CAcreateserial", "-out", cert_path,
            "-extfile", ext_path, "-extensions", "ext"
        ], check=True, capture_output=True)

        return AgentCertBundle(
            ca_cert_path=self.ca_cert,
            agent_cert_path=cert_path,
            agent_key_path=key_path,
            agent_id=agent_id
        )

# Usage at agent startup
def setup_agent_certs(agent_id: str) -> AgentCertBundle:
    ca = LocalCertificateAuthority()
    if not Path(ca.ca_cert).exists():
        ca.initialize()
    return ca.issue_agent_cert(agent_id)
```

---

## Solution 3: Certificate Pinning and Identity Allowlist

```python
import ssl
import hashlib
import base64
from dataclasses import dataclass
from typing import FrozenSet

@dataclass(frozen=True)
class PinnedAgent:
    agent_id: str          # CN in the certificate
    cert_sha256: str       # SHA-256 of DER-encoded cert, base64

class CertificatePinStore:
    """
    Maintains an allowlist of (agent_id, cert_fingerprint) pairs.
    Rejects connections from agents whose cert doesn't match the pin.
    """

    def __init__(self, pins: list[PinnedAgent]):
        self._by_id: dict[str, str] = {p.agent_id: p.cert_sha256 for p in pins}

    def verify_peer(self, peercert_der: bytes, claimed_agent_id: str) -> bool:
        """Verify that the DER cert matches the pinned fingerprint."""
        actual_fp = base64.b64encode(
            hashlib.sha256(peercert_der).digest()
        ).decode()
        expected_fp = self._by_id.get(claimed_agent_id)
        if expected_fp is None:
            print(f"[Pin] Unknown agent: {claimed_agent_id}")
            return False
        if not _constant_time_compare(actual_fp, expected_fp):
            print(f"[Pin] Fingerprint mismatch for {claimed_agent_id}")
            return False
        return True

    def add_pin(self, agent_id: str, cert_der: bytes):
        fp = base64.b64encode(hashlib.sha256(cert_der).digest()).decode()
        self._by_id[agent_id] = fp

    def compute_pin(self, cert_der: bytes) -> str:
        return base64.b64encode(hashlib.sha256(cert_der).digest()).decode()

def _constant_time_compare(a: str, b: str) -> bool:
    import hmac
    return hmac.compare_digest(a.encode(), b.encode())

def extract_cn_from_peercert(peercert: dict) -> str | None:
    """Extract Common Name from the dict returned by ssl socket.getpeercert()."""
    for field_set in peercert.get("subject", []):
        for key, value in field_set:
            if key == "commonName":
                return value
    return None

# aiohttp middleware that enforces pin store
from aiohttp import web

def make_mtls_middleware(pin_store: CertificatePinStore):
    @web.middleware
    async def mtls_middleware(request: web.Request, handler):
        transport = request.transport
        if transport is None:
            raise web.HTTPForbidden(reason="No TLS transport")

        peercert = transport.get_extra_info("peercert")
        if peercert is None:
            raise web.HTTPForbidden(reason="Client certificate required")

        agent_id = extract_cn_from_peercert(peercert)
        if not agent_id:
            raise web.HTTPForbidden(reason="Cannot determine agent identity")

        # Get DER-encoded cert for pinning check
        peercert_der = transport.get_extra_info("peercert_binary", b"")
        if peercert_der and not pin_store.verify_peer(peercert_der, agent_id):
            raise web.HTTPForbidden(reason=f"Certificate pin mismatch for {agent_id}")

        request["agent_id"] = agent_id
        return await handler(request)

    return mtls_middleware
```

---

## Solution 4: SPIFFE/SVID Identity for Agent Mesh

```python
"""
SPIFFE (Secure Production Identity Framework For Everyone) provides workload
identity via SPIFFE Verifiable Identity Documents (SVIDs). Each agent gets a
URI SAN like spiffe://trust-domain/agent/worker-1 embedded in its TLS cert.
This solution shows how to parse and verify SPIFFE identity from mTLS certs.
"""
import ssl
import re
from dataclasses import dataclass
from urllib.parse import urlparse

@dataclass
class SPIFFEIdentity:
    trust_domain: str
    path: str  # e.g., /agent/worker-1

    @classmethod
    def parse(cls, spiffe_uri: str) -> "SPIFFEIdentity":
        parsed = urlparse(spiffe_uri)
        if parsed.scheme != "spiffe":
            raise ValueError(f"Not a SPIFFE URI: {spiffe_uri}")
        return cls(trust_domain=parsed.netloc, path=parsed.path)

    def __str__(self):
        return f"spiffe://{self.trust_domain}{self.path}"

class SPIFFEVerifier:
    def __init__(self, trust_domain: str, allowed_paths: list[str]):
        self.trust_domain = trust_domain
        # Allow glob-style: /agent/* matches /agent/worker-1
        self.allowed_patterns = [re.compile(
            "^" + p.replace("*", "[^/]+") + "$"
        ) for p in allowed_paths]

    def extract_spiffe_id(self, peercert: dict) -> SPIFFEIdentity | None:
        """Extract SPIFFE URI from the SAN extension of the peer certificate."""
        for san_type, san_value in peercert.get("subjectAltName", []):
            if san_type == "URI" and san_value.startswith("spiffe://"):
                try:
                    return SPIFFEIdentity.parse(san_value)
                except ValueError:
                    pass
        return None

    def authorize(self, peercert: dict) -> SPIFFEIdentity:
        """Extract and authorize SPIFFE identity. Raises on failure."""
        identity = self.extract_spiffe_id(peercert)
        if identity is None:
            raise PermissionError("No SPIFFE identity in client certificate")
        if identity.trust_domain != self.trust_domain:
            raise PermissionError(
                f"Wrong trust domain: {identity.trust_domain} != {self.trust_domain}"
            )
        if not any(p.match(identity.path) for p in self.allowed_patterns):
            raise PermissionError(
                f"SPIFFE path not allowed: {identity.path}"
            )
        return identity

# aiohttp handler with SPIFFE authorization
def make_spiffe_handler(verifier: SPIFFEVerifier):
    async def handle(request):
        transport = request.transport
        peercert = transport.get_extra_info("peercert") if transport else None
        if not peercert:
            from aiohttp import web
            raise web.HTTPForbidden(reason="mTLS required")
        try:
            identity = verifier.authorize(peercert)
        except PermissionError as e:
            from aiohttp import web
            raise web.HTTPForbidden(reason=str(e))

        from aiohttp import web
        return web.json_response({"authorized_as": str(identity)})

    return handle
```

---

## Solution 5: Automatic Certificate Rotation

```python
import asyncio
import time
import ssl
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Awaitable

@dataclass
class CertState:
    cert_path: str
    key_path: str
    ca_cert_path: str
    issued_at: float
    valid_days: int

    @property
    def expires_at(self) -> float:
        return self.issued_at + self.valid_days * 86400

    @property
    def rotate_at(self) -> float:
        # Rotate at 75% of lifetime (before expiry)
        return self.issued_at + self.valid_days * 86400 * 0.75

    @property
    def needs_rotation(self) -> bool:
        return time.time() >= self.rotate_at

class AutoRotatingMTLSClient:
    """
    httpx AsyncClient wrapper that automatically rotates the client cert
    before it expires, without dropping in-flight connections.
    """

    def __init__(self, initial_cert: CertState,
                 renew_fn: Callable[[str], Awaitable[CertState]],
                 agent_id: str):
        self._cert = initial_cert
        self._renew_fn = renew_fn
        self._agent_id = agent_id
        self._lock = asyncio.Lock()
        self._client: "httpx.AsyncClient | None" = None

    async def _get_client(self) -> "httpx.AsyncClient":
        import httpx
        async with self._lock:
            if self._cert.needs_rotation:
                print(f"[CertRotation] Rotating certificate for {self._agent_id}")
                new_cert = await self._renew_fn(self._agent_id)
                self._cert = new_cert
                if self._client:
                    await self._client.aclose()
                    self._client = None

            if self._client is None:
                ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                ssl_ctx.load_verify_locations(self._cert.ca_cert_path)
                ssl_ctx.load_cert_chain(self._cert.cert_path, self._cert.key_path)
                ssl_ctx.minimum_version = ssl.TLSVersion.TLSv1_3
                self._client = httpx.AsyncClient(verify=ssl_ctx)

        return self._client

    async def post(self, url: str, **kwargs) -> "httpx.Response":
        client = await self._get_client()
        return await client.post(url, **kwargs)

    async def get(self, url: str, **kwargs) -> "httpx.Response":
        client = await self._get_client()
        return await client.get(url, **kwargs)

    async def aclose(self):
        if self._client:
            await self._client.aclose()

async def rotation_monitor(cert_state: CertState,
                           renew_fn: Callable[[str], Awaitable[CertState]],
                           agent_id: str,
                           check_interval: float = 3600.0) -> None:
    """Background task: check cert expiry and renew proactively."""
    while True:
        await asyncio.sleep(check_interval)
        if cert_state.needs_rotation:
            print(f"[CertRotation] Background renewal for {agent_id}")
            try:
                cert_state = await renew_fn(agent_id)
                print(f"[CertRotation] Renewed. Next rotation at "
                      f"{time.ctime(cert_state.rotate_at)}")
            except Exception as e:
                print(f"[CertRotation] Renewal failed: {e}")
```

---

## Solution 6: Certificate Revocation via OCSP Stapling

```python
import ssl
import asyncio
import hashlib
from dataclasses import dataclass
from typing import set as Set

@dataclass
class RevocationEntry:
    serial_number: str
    revoked_at: float
    reason: str

class OCSPRevocationCache:
    """
    Simple in-process revocation cache.
    In production, query a real OCSP responder or maintain a CRL.
    """

    def __init__(self, refresh_interval: float = 3600.0):
        self._revoked: dict[str, RevocationEntry] = {}
        self.refresh_interval = refresh_interval

    def revoke(self, serial: str, reason: str = "unspecified"):
        import time
        self._revoked[serial.upper()] = RevocationEntry(
            serial_number=serial.upper(),
            revoked_at=time.time(),
            reason=reason
        )

    def is_revoked(self, serial: str) -> bool:
        return serial.upper() in self._revoked

    def get_revocation_info(self, serial: str) -> RevocationEntry | None:
        return self._revoked.get(serial.upper())

def extract_serial_number(peercert: dict) -> str | None:
    """Extract serial number as hex string from peercert dict."""
    serial = peercert.get("serialNumber")
    return str(serial) if serial else None

class RevocationCheckingSSLContext:
    """
    Wraps ssl.SSLContext and adds post-handshake revocation checking.
    """

    def __init__(self, base_ctx: ssl.SSLContext,
                 revocation_cache: OCSPRevocationCache):
        self._ctx = base_ctx
        self._cache = revocation_cache

    def verify_connection(self, peercert: dict) -> bool:
        serial = extract_serial_number(peercert)
        if serial is None:
            print("[OCSP] Cannot verify: no serial number in cert")
            return False
        if self._cache.is_revoked(serial):
            entry = self._cache.get_revocation_info(serial)
            print(f"[OCSP] Certificate REVOKED: serial={serial}, "
                  f"reason={entry.reason if entry else 'unknown'}")
            return False
        return True

# aiohttp middleware combining mTLS + revocation
from aiohttp import web

def make_full_mtls_middleware(revocation_cache: OCSPRevocationCache,
                              allowed_agent_ids: frozenset[str]):
    @web.middleware
    async def middleware(request: web.Request, handler):
        transport = request.transport
        if not transport:
            raise web.HTTPForbidden(reason="No TLS transport")

        peercert = transport.get_extra_info("peercert")
        if not peercert:
            raise web.HTTPForbidden(reason="Client certificate required")

        # Identity check
        subject = dict(x[0] for x in peercert.get("subject", []))
        agent_id = subject.get("commonName", "")
        if agent_id not in allowed_agent_ids:
            raise web.HTTPForbidden(reason=f"Agent '{agent_id}' not allowed")

        # Revocation check
        serial = extract_serial_number(peercert)
        if serial and revocation_cache.is_revoked(serial):
            raise web.HTTPForbidden(reason="Certificate has been revoked")

        request["agent_id"] = agent_id
        request["cert_serial"] = serial
        return await handler(request)

    return middleware
```

---

## Comparison

| Solution | Identity Proof | Revocation | Rotation | Complexity | Best For |
|---|---|---|---|---|---|
| 1. ssl.SSLContext basics | CN from cert | None | Manual | Low | Getting started |
| 2. Local CA + cert issuance | CN from cert | None | Manual | Med | Dev/staging clusters |
| 3. Certificate pinning | CN + fingerprint | Pin removal | Pin update | Med | High-security, small fleets |
| 4. SPIFFE/SVID | SPIFFE URI | Trust domain | SPIRE daemon | High | Service mesh, k8s |
| 5. Auto-rotation client | CN from cert | None | Automatic (75% lifetime) | Med | Production agents |
| 6. OCSP revocation | CN + serial | OCSP cache | Manual | Med-High | Compliance requirements |

**Key principle**: mTLS moves authentication from "possession of a shared secret" to "possession of a private key whose corresponding certificate was signed by a trusted CA." Compromising a token grants access to all services; compromising a private key only affects that one agent's identity and can be revoked.
