---
title: "Agent Doesn't Implement Signed Tool Manifests for Supply Chain Integrity"
description: "AI agents that load tool definitions from files, registries, or remote endpoints at startup have no way to verify those definitions haven't been tampered with. A signed tool manifest attaches a cryptographic signature to the full tool schema at build time; the agent verifies the signature before registering any tool, blocking supply-chain attacks that substitute malicious tool definitions between build and deploy."
date: 2025-02-14
difficulty: advanced
category: security
slug: agent-doesnt-implement-signed-tool-manifests-for-supply-chain-integrity
tags:
  - supply-chain
  - tool-manifest
  - signing
  - ed25519
  - integrity
  - security
  - tool-registry
symptoms:
  - "Tool definitions are loaded from a shared config file with no integrity check"
  - "An attacker who writes to the tool registry directory can substitute a malicious tool schema"
  - "Agent downloads tool manifests from an S3 bucket without verifying signatures"
  - "No record of which tool schema version was active when an incident occurred"
  - "CI pipeline can push unsigned tool definitions directly to production"
---

## Problem

Tool schemas (name, description, parameters) are the interface between an agent and the capabilities it can invoke. If those schemas can be modified after the developer signed off — by a compromised CI step, a writable shared volume, or a malicious package update — the agent will execute attacker-chosen logic while the developer's approved schema is shown in logs. Signing manifests at build time and verifying at load time creates a cryptographic chain of custody: the agent refuses to load any tool whose schema doesn't match the signature produced by the authorised signer key.

---

## Solution 1: ToolManifestSigner — Ed25519 Manifest Signing

```python
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

try:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey, Ed25519PublicKey,
    )
    from cryptography.hazmat.primitives.serialization import (
        Encoding, PublicFormat, PrivateFormat, NoEncryption,
    )
    import base64
    _CRYPTO = True
except ImportError:
    _CRYPTO = False


@dataclass
class ToolDefinition:
    name: str
    description: str
    parameters: Dict[str, Any]
    version: str = "1.0.0"
    tags: List[str] = field(default_factory=list)


@dataclass
class SignedManifest:
    tools: List[ToolDefinition]
    signer_key_id: str
    signed_at: float
    manifest_hash: str     # SHA-256 of canonical JSON
    signature: str         # base64-encoded Ed25519 signature


class ToolManifestSigner:
    """
    Signs and verifies tool manifests using Ed25519.
    The private key is held by CI/CD; the public key is embedded in the agent.

    Usage (in CI):
        signer = ToolManifestSigner.generate_keypair()
        manifest = signer.sign(tools, key_id="ci-prod-2025")
        signer.save_manifest(manifest, "tools.manifest.json")

    Usage (in agent):
        verifier = ToolManifestSigner(public_key_pem=PUBLIC_KEY_PEM)
        tools = verifier.verify_and_load("tools.manifest.json")
    """

    def __init__(self, private_key: Optional[Any] = None,
                 public_key: Optional[Any] = None,
                 public_key_pem: Optional[str] = None):
        if not _CRYPTO:
            raise RuntimeError("pip install cryptography")
        self._private_key = private_key
        if public_key_pem and not public_key:
            from cryptography.hazmat.primitives.serialization import load_pem_public_key
            self._public_key = load_pem_public_key(public_key_pem.encode())
        else:
            self._public_key = public_key

    @classmethod
    def generate_keypair(cls) -> "ToolManifestSigner":
        if not _CRYPTO:
            raise RuntimeError("pip install cryptography")
        private_key = Ed25519PrivateKey.generate()
        public_key = private_key.public_key()
        return cls(private_key=private_key, public_key=public_key)

    def export_public_key_pem(self) -> str:
        return self._public_key.public_bytes(
            Encoding.PEM, PublicFormat.SubjectPublicKeyInfo
        ).decode()

    def _canonical_json(self, tools: List[ToolDefinition]) -> bytes:
        data = [
            {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
                "version": t.version,
                "tags": sorted(t.tags),
            }
            for t in sorted(tools, key=lambda t: t.name)
        ]
        return json.dumps(data, sort_keys=True, separators=(",", ":")).encode()

    def sign(self, tools: List[ToolDefinition], key_id: str) -> SignedManifest:
        if not self._private_key:
            raise RuntimeError("Private key required to sign")
        canonical = self._canonical_json(tools)
        digest = hashlib.sha256(canonical).hexdigest()
        raw_sig = self._private_key.sign(canonical)
        sig_b64 = base64.b64encode(raw_sig).decode()
        return SignedManifest(
            tools=tools,
            signer_key_id=key_id,
            signed_at=time.time(),
            manifest_hash=digest,
            signature=sig_b64,
        )

    def verify(self, manifest: SignedManifest) -> bool:
        if not self._public_key:
            raise RuntimeError("Public key required to verify")
        canonical = self._canonical_json(manifest.tools)
        # Verify hash first (fast)
        if hashlib.sha256(canonical).hexdigest() != manifest.manifest_hash:
            return False
        try:
            sig_bytes = base64.b64decode(manifest.signature)
            self._public_key.verify(sig_bytes, canonical)
            return True
        except Exception:
            return False

    def save_manifest(self, manifest: SignedManifest, path: str):
        data = {
            "tools": [
                {"name": t.name, "description": t.description,
                 "parameters": t.parameters, "version": t.version,
                 "tags": t.tags}
                for t in manifest.tools
            ],
            "signer_key_id": manifest.signer_key_id,
            "signed_at": manifest.signed_at,
            "manifest_hash": manifest.manifest_hash,
            "signature": manifest.signature,
        }
        with open(path, "w") as f:
            json.dump(data, f, indent=2)

    def load_manifest(self, path: str) -> SignedManifest:
        with open(path) as f:
            data = json.load(f)
        tools = [
            ToolDefinition(
                name=t["name"], description=t["description"],
                parameters=t["parameters"],
                version=t.get("version", "1.0.0"),
                tags=t.get("tags", []),
            )
            for t in data["tools"]
        ]
        return SignedManifest(
            tools=tools,
            signer_key_id=data["signer_key_id"],
            signed_at=data["signed_at"],
            manifest_hash=data["manifest_hash"],
            signature=data["signature"],
        )
```

---

## Solution 2: VerifyingToolRegistry — Refuse Unsigned Tools at Load Time

```python
import logging
import time
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


class VerifyingToolRegistry:
    """
    Tool registry that only accepts tools from verified signed manifests.
    Any attempt to register a tool without a valid manifest signature raises
    SecurityError, blocking unsigned tools from reaching the agent.

    Usage:
        registry = VerifyingToolRegistry(
            signer=ToolManifestSigner(public_key_pem=PUBLIC_KEY_PEM),
            enforce=True,
        )
        registry.load_from_manifest("tools.manifest.json")
        tool_fn = registry.get("web_search")
    """

    def __init__(self, signer: ToolManifestSigner, enforce: bool = True):
        self._signer = signer
        self._enforce = enforce
        self._tools: Dict[str, ToolDefinition] = {}
        self._fns: Dict[str, Callable] = {}
        self._load_time: Optional[float] = None
        self._manifest_hash: Optional[str] = None

    def load_from_manifest(self, path: str,
                            implementations: Optional[Dict[str, Callable]] = None):
        manifest = self._signer.load_manifest(path)
        valid = self._signer.verify(manifest)
        if not valid:
            msg = f"Manifest signature verification FAILED for {path}"
            logger.critical(msg)
            if self._enforce:
                raise SecurityError(msg)
            logger.warning("enforce=False — loading unverified manifest")

        for tool in manifest.tools:
            self._tools[tool.name] = tool
        if implementations:
            self._fns.update(implementations)
        self._load_time = time.time()
        self._manifest_hash = manifest.manifest_hash
        logger.info(
            "manifest_loaded path=%s tools=%d hash=%s signer=%s",
            path, len(manifest.tools),
            manifest.manifest_hash[:12], manifest.signer_key_id,
        )

    def register_implementation(self, tool_name: str, fn: Callable):
        if tool_name not in self._tools:
            raise KeyError(
                f"Tool '{tool_name}' not in manifest — "
                "add it to the manifest before registering an implementation"
            )
        self._fns[tool_name] = fn

    def get(self, tool_name: str) -> Callable:
        fn = self._fns.get(tool_name)
        if fn is None:
            raise KeyError(f"No implementation for tool '{tool_name}'")
        return fn

    def schema_for(self, tool_name: str) -> ToolDefinition:
        return self._tools[tool_name]

    def all_schemas(self) -> List[ToolDefinition]:
        return list(self._tools.values())

    def integrity_report(self) -> Dict[str, Any]:
        return {
            "tool_count": len(self._tools),
            "manifest_hash": self._manifest_hash,
            "load_time": self._load_time,
            "enforce": self._enforce,
        }


class SecurityError(Exception):
    pass
```

---

## Solution 3: ManifestVersionPinner — Prevent Downgrade Attacks

```python
import json
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)


class ManifestVersionPinner:
    """
    Tracks the last-seen manifest hash in a local pin file.
    Rejects manifests whose hash is older than (or different from) the pinned
    version, preventing rollback attacks where an attacker substitutes an older
    valid manifest that was signed before a security fix.

    Usage:
        pinner = ManifestVersionPinner(pin_file=".manifest.pin")

        # On first load: stores manifest hash in pin file
        # On subsequent loads: verifies hash matches or is newer
        pinner.check_and_pin(manifest)
    """

    def __init__(self, pin_file: str = ".manifest.pin"):
        self._pin_file = pin_file

    def _load_pin(self) -> Optional[dict]:
        if not os.path.exists(self._pin_file):
            return None
        try:
            with open(self._pin_file) as f:
                return json.load(f)
        except Exception:
            return None

    def _save_pin(self, manifest: SignedManifest):
        with open(self._pin_file, "w") as f:
            json.dump({
                "hash": manifest.manifest_hash,
                "signed_at": manifest.signed_at,
                "signer_key_id": manifest.signer_key_id,
            }, f)

    def check_and_pin(self, manifest: SignedManifest,
                       allow_rollback: bool = False):
        pin = self._load_pin()
        if pin is None:
            logger.info("manifest_pin_created hash=%s", manifest.manifest_hash[:12])
            self._save_pin(manifest)
            return

        if not allow_rollback and manifest.signed_at < pin["signed_at"]:
            raise SecurityError(
                f"Manifest rollback detected: incoming signed_at={manifest.signed_at:.0f} "
                f"< pinned signed_at={pin['signed_at']:.0f}"
            )

        if manifest.manifest_hash != pin["hash"]:
            logger.info(
                "manifest_hash_changed old=%s new=%s",
                pin["hash"][:12], manifest.manifest_hash[:12],
            )
            self._save_pin(manifest)
```

---

## Solution 4: RemoteManifestFetcher — Fetch and Verify from Artifact Store

```python
import hashlib
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class RemoteManifestFetcher:
    """
    Downloads a signed manifest from a remote URL (S3, GCS, HTTPS)
    and verifies its signature before returning it to the registry.

    Usage:
        fetcher = RemoteManifestFetcher(
            signer=ToolManifestSigner(public_key_pem=PUBLIC_KEY_PEM),
        )
        manifest = await fetcher.fetch(
            "https://artifacts.example.com/tools/prod.manifest.json"
        )
        registry.load_from_manifest_obj(manifest)
    """

    def __init__(self, signer: ToolManifestSigner,
                 expected_hash: Optional[str] = None):
        self._signer = signer
        self._expected_hash = expected_hash

    async def fetch(self, url: str) -> SignedManifest:
        try:
            import httpx
        except ImportError:
            raise RuntimeError("pip install httpx")

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            raw = resp.content

        # Optional: verify SHA-256 of the downloaded bytes matches
        # a hash pinned in infrastructure (e.g., Terraform state)
        if self._expected_hash:
            actual = hashlib.sha256(raw).hexdigest()
            if actual != self._expected_hash:
                raise SecurityError(
                    f"Manifest download hash mismatch: expected {self._expected_hash} "
                    f"got {actual}"
                )

        import json, tempfile, os
        with tempfile.NamedTemporaryFile(delete=False, suffix=".json") as tmp:
            tmp.write(raw)
            tmp_path = tmp.name

        try:
            manifest = self._signer.load_manifest(tmp_path)
        finally:
            os.unlink(tmp_path)

        if not self._signer.verify(manifest):
            raise SecurityError(f"Remote manifest from {url} failed signature check")

        logger.info("remote_manifest_verified url=%s hash=%s",
                    url, manifest.manifest_hash[:12])
        return manifest
```

---

## Solution 5: ManifestAuditLogger — Immutable Load/Reject Audit Trail

```python
import json
import logging
import os
import time
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


class ManifestAuditLogger:
    """
    Writes an append-only JSONL audit log of every manifest load,
    verification failure, and tool registration. Provides forensic
    evidence of which tool schema was active at any point in time.

    Usage:
        audit = ManifestAuditLogger(log_path="/var/log/agent/manifest-audit.jsonl")
        audit.record_load(manifest, verified=True, source="s3://bucket/tools.json")
        audit.record_rejection(manifest, reason="signature_invalid")
    """

    def __init__(self, log_path: str = "manifest-audit.jsonl"):
        self._path = log_path

    def _append(self, record: Dict[str, Any]):
        record["ts"] = time.time()
        with open(self._path, "a") as f:
            f.write(json.dumps(record) + "\n")

    def record_load(self, manifest: SignedManifest,
                    verified: bool, source: str):
        self._append({
            "event": "manifest_loaded",
            "verified": verified,
            "source": source,
            "hash": manifest.manifest_hash,
            "signer_key_id": manifest.signer_key_id,
            "signed_at": manifest.signed_at,
            "tool_names": [t.name for t in manifest.tools],
        })
        logger.info("audit_manifest_loaded hash=%s verified=%s",
                    manifest.manifest_hash[:12], verified)

    def record_rejection(self, manifest: SignedManifest, reason: str):
        self._append({
            "event": "manifest_rejected",
            "reason": reason,
            "hash": manifest.manifest_hash,
            "signer_key_id": manifest.signer_key_id,
        })
        logger.critical("audit_manifest_rejected reason=%s hash=%s",
                         reason, manifest.manifest_hash[:12])

    def record_tool_invocation(self, tool_name: str, manifest_hash: str):
        self._append({
            "event": "tool_invoked",
            "tool_name": tool_name,
            "manifest_hash": manifest_hash,
        })

    def recent_events(self, n: int = 50) -> List[Dict]:
        if not os.path.exists(self._path):
            return []
        with open(self._path) as f:
            lines = f.readlines()
        return [json.loads(l) for l in lines[-n:]]
```

---

## Solution 6: ManifestPipeline — Full Sign-Verify-Load Stack

```python
import logging
from typing import Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


class ManifestPipeline:
    """
    Integrates signing, verification, version pinning, and audit logging
    into a single manifest lifecycle manager.

    In CI (build time):
        pipeline = ManifestPipeline.for_signing(private_key_pem=PRIVATE_KEY)
        pipeline.sign_and_save(tools, key_id="ci-2025-q1", out_path="dist/tools.json")

    In agent (runtime):
        pipeline = ManifestPipeline.for_verification(
            public_key_pem=PUBLIC_KEY_PEM,
            audit_log="/var/log/agent/manifest.jsonl",
        )
        registry = pipeline.load("dist/tools.json", implementations=impl_map)
    """

    def __init__(self, signer: ToolManifestSigner,
                 pinner: Optional[ManifestVersionPinner] = None,
                 audit: Optional[ManifestAuditLogger] = None):
        self._signer = signer
        self._pinner = pinner
        self._audit = audit

    @classmethod
    def for_signing(cls, private_key_pem: str) -> "ManifestPipeline":
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
        pk = load_pem_private_key(private_key_pem.encode(), password=None)
        pub = pk.public_key()
        signer = ToolManifestSigner(private_key=pk, public_key=pub)
        return cls(signer)

    @classmethod
    def for_verification(cls, public_key_pem: str,
                          pin_file: str = ".manifest.pin",
                          audit_log: Optional[str] = None) -> "ManifestPipeline":
        signer = ToolManifestSigner(public_key_pem=public_key_pem)
        pinner = ManifestVersionPinner(pin_file)
        audit = ManifestAuditLogger(audit_log) if audit_log else None
        return cls(signer, pinner, audit)

    def sign_and_save(self, tools: List[ToolDefinition],
                       key_id: str, out_path: str):
        manifest = self._signer.sign(tools, key_id)
        self._signer.save_manifest(manifest, out_path)
        logger.info("manifest_signed hash=%s path=%s",
                     manifest.manifest_hash[:12], out_path)

    def load(self, path: str,
              implementations: Optional[Dict[str, Callable]] = None,
              source: str = "local") -> VerifyingToolRegistry:
        manifest = self._signer.load_manifest(path)
        valid = self._signer.verify(manifest)

        if self._pinner and valid:
            self._pinner.check_and_pin(manifest)

        if self._audit:
            if valid:
                self._audit.record_load(manifest, True, source)
            else:
                self._audit.record_rejection(manifest, "signature_invalid")

        registry = VerifyingToolRegistry(self._signer, enforce=True)
        registry.load_from_manifest(path, implementations)
        return registry
```

---

## Comparison

| Approach | Signs | Verifies | Rollback Protection | Remote Fetch | Audit Trail |
|---|---|---|---|---|---|
| **ToolManifestSigner** | Yes | Yes | No | No | No |
| **VerifyingToolRegistry** | No | Yes (at load) | No | No | No |
| **ManifestVersionPinner** | No | No | Yes | No | No |
| **RemoteManifestFetcher** | No | Yes | No | Yes | No |
| **ManifestAuditLogger** | No | No | No | No | Yes |
| **ManifestPipeline** | Yes | Yes | Yes | Optional | Yes |

**Key insight**: the private signing key must never be present at agent runtime — it belongs in CI. The agent embeds only the public key as a build-time constant. At startup, the agent calls `ManifestPipeline.for_verification()`, which verifies the signature, checks the version pin (rejecting rollbacks), logs the load event, and populates the registry — all before any tool function is callable. This ensures the only path to registering a tool is through a signature chain that terminates at a key controlled by your engineering team.
