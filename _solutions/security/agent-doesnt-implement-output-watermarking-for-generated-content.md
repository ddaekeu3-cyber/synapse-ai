---
title: "Agent Doesn't Implement Output Watermarking for Generated Content"
description: "Agents that produce generated text, reports, or documents with no provenance markers make it impossible to trace content back to the generating session, model version, or user request — enabling plagiarism, content repudiation, and undetected model poisoning. Implement output watermarking that embeds cryptographically verifiable provenance metadata into generated content without visibly altering it, using steganographic whitespace encoding and a signed metadata footer."
date: 2026-04-16
difficulty: advanced
category: security
slug: agent-doesnt-implement-output-watermarking-for-generated-content
tags: [watermarking, provenance, content-integrity, steganography, output-signing, generated-content]
symptoms:
  - "No way to determine which agent session or model version produced a given document"
  - "Generated content is repudiated by users claiming they did not request it"
  - "Cannot distinguish between original agent output and tampered copies in audits"
  - "Model poisoning suspected but no provenance trail to confirm which outputs are affected"
  - "Compliance requires proof of AI-generated content origin but no mechanism exists"
---

## Why This Happens

Generated text is indistinguishable from human-written text once it leaves the agent. Without embedded provenance, any post-hoc attribution relies on external logs that can be deleted, forged, or lost. Watermarking addresses this by embedding verifiable metadata inside the content itself. Two complementary techniques are used: steganographic encoding (invisible to human readers, survives copy-paste) and a signed metadata block (machine-verifiable, survives reformatting). Neither technique alone is sufficient — steganography is fragile to reformatting, and signed footers can be stripped. Together they provide redundant provenance that degrades gracefully.

## Solution 1: Provenance Metadata

```python
import hashlib
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ContentProvenance:
    session_id: str
    user_id: str
    model_version: str
    request_id: str
    generated_at: float = field(default_factory=time.time)
    agent_version: str = "1.0"
    content_hash: str = ""       # SHA-256 of raw content, set after generation

    def fingerprint(self) -> str:
        """Short identifier for logs — not a security primitive."""
        key = f"{self.session_id}:{self.request_id}:{self.generated_at}"
        return hashlib.sha256(key.encode()).hexdigest()[:16]

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "user_id": self.user_id,
            "model_version": self.model_version,
            "request_id": self.request_id,
            "generated_at": self.generated_at,
            "agent_version": self.agent_version,
            "content_hash": self.content_hash,
        }
```

## Solution 2: Steganographic Whitespace Encoder

```python
import re
from typing import Tuple


class WhitespaceWatermarkEncoder:
    """
    Encodes a short bit string into trailing whitespace on paragraph lines.
    Bit 0 → single trailing space; Bit 1 → double trailing space.
    Invisible to readers, survives most copy-paste operations.
    Max payload: one bit per paragraph-ending line.
    """

    ZERO_MARK = " "
    ONE_MARK = "  "

    def encode(self, text: str, payload_bits: str) -> str:
        """
        payload_bits: string of '0' and '1', e.g. '10110010'
        Returns text with bits encoded in trailing whitespace on lines.
        """
        lines = text.split("\n")
        bit_idx = 0
        result = []
        for line in lines:
            stripped = line.rstrip()
            if stripped and bit_idx < len(payload_bits):
                marker = self.ONE_MARK if payload_bits[bit_idx] == "1" else self.ZERO_MARK
                result.append(stripped + marker)
                bit_idx += 1
            else:
                result.append(line)
        return "\n".join(result)

    def decode(self, text: str) -> str:
        """Extract bit string from trailing whitespace."""
        bits = []
        for line in text.split("\n"):
            if not line.rstrip():
                continue
            trailing = line[len(line.rstrip()):]
            if trailing == self.ONE_MARK:
                bits.append("1")
            elif trailing == self.ZERO_MARK:
                bits.append("0")
        return "".join(bits)

    @staticmethod
    def bytes_to_bits(data: bytes) -> str:
        return "".join(f"{b:08b}" for b in data)

    @staticmethod
    def bits_to_bytes(bits: str) -> bytes:
        padded = bits + "0" * (8 - len(bits) % 8) if len(bits) % 8 else bits
        return bytes(int(padded[i:i+8], 2) for i in range(0, len(padded), 8))
```

## Solution 3: HMAC Provenance Signer

```python
import base64
import hashlib
import hmac
import json
import time
from typing import Optional, Tuple


class ProvenanceSigner:
    """
    Signs provenance metadata with HMAC-SHA256 using a shared secret.
    The signature is embedded in a structured footer block.
    """

    FOOTER_DELIMITER = "\n\n---\n"

    def __init__(self, secret_key: bytes):
        self._key = secret_key

    def _sign(self, payload: dict) -> str:
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        sig = hmac.new(self._key, canonical.encode(), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(sig).decode()

    def attach_footer(self, content: str, provenance: ContentProvenance) -> str:
        """Append a signed provenance footer to the generated content."""
        provenance.content_hash = hashlib.sha256(content.encode()).hexdigest()
        payload = provenance.to_dict()
        signature = self._sign(payload)
        footer = {
            "provenance": payload,
            "signature": signature,
        }
        footer_str = json.dumps(footer, separators=(",", ":"))
        encoded = base64.urlsafe_b64encode(footer_str.encode()).decode()
        return content + self.FOOTER_DELIMITER + f"<!-- watermark:{encoded} -->"

    def verify_footer(self, content_with_footer: str) -> Tuple[bool, Optional[ContentProvenance]]:
        """
        Extract and verify the signed footer.
        Returns (valid, provenance) or (False, None) if tampered/missing.
        """
        parts = content_with_footer.rsplit(self.FOOTER_DELIMITER, 1)
        if len(parts) != 2:
            return False, None
        raw_content, footer_block = parts
        import re
        m = re.search(r"<!-- watermark:([A-Za-z0-9_\-=]+) -->", footer_block)
        if not m:
            return False, None
        try:
            footer_json = base64.urlsafe_b64decode(m.group(1)).decode()
            footer = json.loads(footer_json)
            payload = footer["provenance"]
            expected_sig = self._sign(payload)
            if not hmac.compare_digest(expected_sig, footer["signature"]):
                return False, None
            # Verify content hash
            actual_hash = hashlib.sha256(raw_content.encode()).hexdigest()
            if actual_hash != payload.get("content_hash"):
                return False, None
            prov = ContentProvenance(**{
                k: v for k, v in payload.items()
                if k in ContentProvenance.__dataclass_fields__
            })
            return True, prov
        except Exception:
            return False, None
```

## Solution 4: Watermarking Pipeline

```python
import hashlib


class OutputWatermarkingPipeline:
    """
    Combines steganographic whitespace encoding with a signed footer.
    Embeds a truncated fingerprint into whitespace and the full
    provenance into the footer for redundant provenance.
    """

    STEG_PAYLOAD_BYTES = 4    # 32 bits → 32 lines of whitespace capacity needed

    def __init__(
        self,
        signer: ProvenanceSigner,
        encoder: WhitespaceWatermarkEncoder,
    ):
        self._signer = signer
        self._encoder = encoder

    def watermark(self, content: str, provenance: ContentProvenance) -> str:
        # Step 1: Embed fingerprint bits into whitespace
        fp_bytes = bytes.fromhex(provenance.fingerprint()[:self.STEG_PAYLOAD_BYTES * 2])
        bits = self._encoder.bytes_to_bits(fp_bytes)
        content_with_steg = self._encoder.encode(content, bits)

        # Step 2: Attach signed footer
        content_final = self._signer.attach_footer(content_with_steg, provenance)
        return content_final

    def verify(self, watermarked_content: str) -> dict:
        valid, provenance = self._signer.verify_footer(watermarked_content)
        steg_bits = self._encoder.decode(watermarked_content)

        result = {
            "footer_valid": valid,
            "provenance": provenance.to_dict() if provenance else None,
            "steg_bits_present": len(steg_bits) >= self.STEG_PAYLOAD_BYTES * 8,
            "steg_bits_sample": steg_bits[:32],
        }
        if valid and provenance:
            expected_bits = self._encoder.bytes_to_bits(
                bytes.fromhex(provenance.fingerprint()[:self.STEG_PAYLOAD_BYTES * 2])
            )
            result["steg_matches_footer"] = steg_bits.startswith(expected_bits[:len(steg_bits)])
        return result
```

## Solution 5: Watermark Provenance Registry

```python
import time
from threading import Lock
from typing import Dict, List, Optional


class WatermarkProvenanceRegistry:
    """
    Maintains an in-memory + optional file index of all watermarked outputs.
    Allows lookup by fingerprint for tamper investigations.
    """

    def __init__(self, max_entries: int = 50000):
        self._max = max_entries
        self._index: Dict[str, dict] = {}
        self._lock = Lock()

    def register(self, provenance: ContentProvenance) -> None:
        with self._lock:
            if len(self._index) >= self._max:
                oldest_key = min(self._index, key=lambda k: self._index[k]["registered_at"])
                del self._index[oldest_key]
            self._index[provenance.fingerprint()] = {
                "provenance": provenance.to_dict(),
                "registered_at": time.time(),
            }

    def lookup(self, fingerprint: str) -> Optional[dict]:
        with self._lock:
            return self._index.get(fingerprint)

    def recent(self, limit: int = 20) -> List[dict]:
        with self._lock:
            items = sorted(
                self._index.values(),
                key=lambda x: x["registered_at"],
                reverse=True,
            )
        return items[:limit]
```

## Solution 6: Watermark Integrity Auditor

```python
import time
from typing import List


class WatermarkIntegrityAuditor:
    """
    Batch-verifies a set of watermarked documents and reports
    integrity status, provenance mismatches, and stripped watermarks.
    """

    def __init__(self, pipeline: OutputWatermarkingPipeline):
        self._pipeline = pipeline

    def audit(self, documents: List[str]) -> dict:
        results = []
        valid_count = 0
        tampered_count = 0
        missing_count = 0

        for i, doc in enumerate(documents):
            verification = self._pipeline.verify(doc)
            status = "valid"
            if not verification["footer_valid"]:
                if "<!-- watermark:" in doc:
                    status = "tampered"
                    tampered_count += 1
                else:
                    status = "missing"
                    missing_count += 1
            else:
                valid_count += 1
                if not verification.get("steg_matches_footer", True):
                    status = "steg_mismatch"
                    tampered_count += 1
                    valid_count -= 1

            results.append({
                "document_index": i,
                "status": status,
                "provenance": verification.get("provenance"),
            })

        return {
            "audited_at": time.time(),
            "total": len(documents),
            "valid": valid_count,
            "tampered": tampered_count,
            "missing_watermark": missing_count,
            "integrity_rate": round(valid_count / max(len(documents), 1), 4),
            "results": results,
        }
```

## Comparison

| Approach | Steg Encoding | Signed Footer | Verification | Registry | Batch Audit |
|---|---|---|---|---|---|
| WhitespaceWatermarkEncoder | Yes (whitespace bits) | No | Decode only | No | No |
| ProvenanceSigner | No | Yes (HMAC-SHA256) | Yes (sig + hash) | No | No |
| OutputWatermarkingPipeline | Via encoder | Via signer | Yes (both layers) | No | No |
| WatermarkProvenanceRegistry | No | No | No | Yes | No |
| WatermarkIntegrityAuditor | No | No | Via pipeline | No | Yes |

**Best for production**: Always combine both layers — whitespace encoding survives copy-paste but not reformatting; the signed footer survives reformatting but can be stripped. A document that passes footer verification but fails steganographic correlation indicates the footer was copied from a different document (content substitution attack). Rotate the HMAC signing key on a schedule and keep an index of key versions so older documents can still be verified. Store all provenance records in `WatermarkProvenanceRegistry` at generation time — if a watermark is later removed or tampered, the registry provides the authoritative source of truth for audits.
