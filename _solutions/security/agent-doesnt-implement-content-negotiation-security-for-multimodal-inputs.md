---
title: "Agent Doesn't Implement Content Negotiation Security for Multimodal Inputs"
description: "AI agents that accept multimodal inputs — images, PDFs, audio, documents — without MIME type validation and content inspection are vulnerable to polyglot files, MIME confusion attacks, and steganographic prompt injection. An attacker uploads a JPEG that is simultaneously a valid ZIP containing malicious content, or embeds adversarial instructions in image metadata. Secure content negotiation validates declared MIME type against actual file magic bytes, enforces format allowlists, and strips metadata before forwarding to the LLM."
date: 2025-02-13
difficulty: intermediate
category: security
slug: agent-doesnt-implement-content-negotiation-security-for-multimodal-inputs
tags:
  - content-negotiation
  - mime-type
  - multimodal
  - file-upload
  - polyglot
  - steganography
  - prompt-injection
  - security
symptoms:
  - "Agent accepts any MIME type that the client declares without verifying the actual file content"
  - "Image uploads are passed directly to vision model without stripping EXIF metadata"
  - "PDF tool processes files without checking if the file is actually a PDF"
  - "Agent can be fed a polyglot file that is a valid image AND a valid executable"
  - "Adversarial prompt text hidden in image metadata reaches the LLM context"
---

## Problem

Multimodal agents are vulnerable to three distinct attacks via file inputs:
1. **MIME confusion**: client declares `image/jpeg` but uploads a ZIP or executable; agent forwards it to an image processing tool that may parse the ZIP.
2. **Polyglot files**: a file is simultaneously a valid JPEG and a valid PDF/ZIP, exploiting format parsers with different entry points.
3. **Metadata injection**: EXIF, XMP, or PDF metadata contains adversarial text that reaches the LLM context as "extracted content".

Secure content negotiation validates magic bytes against declared MIME, enforces a format allowlist, limits file size, and strips metadata before processing.

---

## Solution 1: MagicBytesValidator — Verify Actual File Format

```python
import io
from dataclasses import dataclass
from typing import Dict, FrozenSet, List, Optional, Tuple


# Magic byte signatures: (offset, bytes_pattern)
_MAGIC: Dict[str, List[Tuple[int, bytes]]] = {
    "image/jpeg": [(0, b"\xff\xd8\xff")],
    "image/png":  [(0, b"\x89PNG\r\n\x1a\n")],
    "image/gif":  [(0, b"GIF87a"), (0, b"GIF89a")],
    "image/webp": [(0, b"RIFF"), (8, b"WEBP")],
    "application/pdf": [(0, b"%PDF-")],
    "audio/mpeg": [(0, b"\xff\xfb"), (0, b"\xff\xf3"), (0, b"ID3")],
    "audio/wav":  [(0, b"RIFF"), (8, b"WAVE")],
    "text/plain": [],  # no magic — validate by UTF-8 decode attempt
}

_POLYGLOT_INDICATORS: List[bytes] = [
    b"PK\x03\x04",       # ZIP signature (inside JPEG could be polyglot)
    b"MZ",               # PE executable
    b"\x7fELF",          # ELF executable
    b"#!/",              # shebang
    b"<script",          # HTML/JS
    b"<?php",            # PHP
]


@dataclass
class ContentValidationResult:
    declared_mime: str
    detected_mime: Optional[str]
    valid: bool
    reason: Optional[str] = None
    polyglot_suspected: bool = False


class MagicBytesValidator:
    """
    Validates that a file's actual content matches its declared MIME type.
    Detects polyglot files and common disguised payloads.

    Usage:
        validator = MagicBytesValidator(allowed_mimes={"image/jpeg", "image/png", "application/pdf"})
        result = validator.validate(file_bytes, declared_mime="image/jpeg")
        if not result.valid:
            raise UnsafeContent(result.reason)
    """

    def __init__(self, allowed_mimes: Optional[FrozenSet[str]] = None):
        self._allowed = allowed_mimes or frozenset(_MAGIC.keys())

    def _detect_mime(self, data: bytes) -> Optional[str]:
        for mime, signatures in _MAGIC.items():
            if not signatures:
                continue
            matched = True
            for offset, pattern in signatures:
                if data[offset:offset + len(pattern)] != pattern:
                    matched = False
                    break
            if matched:
                return mime
        return None

    def _detect_polyglot(self, data: bytes) -> bool:
        for sig in _POLYGLOT_INDICATORS:
            if data.find(sig, 16) != -1:  # skip first 16 bytes (may be legit header)
                return True
        return False

    def validate(self, data: bytes, declared_mime: str) -> ContentValidationResult:
        declared_mime = declared_mime.split(";")[0].strip().lower()
        if declared_mime not in self._allowed:
            return ContentValidationResult(
                declared_mime, None, False,
                f"MIME type '{declared_mime}' not in allowlist"
            )
        detected = self._detect_mime(data)
        if detected and detected != declared_mime:
            return ContentValidationResult(
                declared_mime, detected, False,
                f"Magic bytes indicate '{detected}', declared '{declared_mime}' — MIME mismatch"
            )
        polyglot = self._detect_polyglot(data)
        if polyglot:
            return ContentValidationResult(
                declared_mime, detected, False,
                "Polyglot indicators found — file may be a disguised executable or archive",
                polyglot_suspected=True,
            )
        return ContentValidationResult(declared_mime, detected, True)
```

---

## Solution 2: MetadataStripper — Remove EXIF, XMP, and PDF Metadata

```python
import io
from typing import Optional


class ImageMetadataStripper:
    """
    Strips EXIF, XMP, IPTC, and comment metadata from images.
    Prevents adversarial text hidden in metadata from reaching the LLM.

    Usage:
        stripper = ImageMetadataStripper()
        clean_bytes = stripper.strip_jpeg(jpeg_bytes)
        # All EXIF/XMP removed; pixel data preserved
    """

    def strip_jpeg(self, data: bytes) -> bytes:
        """Strip all JPEG APP markers (APP0–APP15) except APP0 (JFIF)."""
        out = io.BytesIO()
        i = 0
        if data[:2] != b"\xff\xd8":
            raise ValueError("Not a valid JPEG")
        out.write(b"\xff\xd8")
        i = 2
        while i < len(data):
            if data[i] != 0xff:
                break
            marker = data[i:i + 2]
            if len(marker) < 2:
                break
            # Read segment length
            if i + 4 > len(data):
                break
            length = int.from_bytes(data[i + 2:i + 4], "big")
            seg_end = i + 2 + length
            # Skip APP1–APP15 (EXIF, XMP, ICC, etc.); keep APP0 (JFIF) and SOS onwards
            marker_byte = data[i + 1]
            if 0xe1 <= marker_byte <= 0xef:  # APP1–APP15
                i = seg_end
                continue
            if marker_byte == 0xfe:  # COM (comment)
                i = seg_end
                continue
            if marker_byte == 0xda:  # SOS — start of scan; rest is image data
                out.write(data[i:])
                break
            out.write(data[i:seg_end])
            i = seg_end
        return out.getvalue()

    def strip_png(self, data: bytes) -> bytes:
        """Strip text chunks (tEXt, iTXt, zTXt) from PNG."""
        try:
            import struct
            if data[:8] != b"\x89PNG\r\n\x1a\n":
                raise ValueError("Not a valid PNG")
            out = io.BytesIO()
            out.write(data[:8])
            i = 8
            STRIP_TYPES = {b"tEXt", b"iTXt", b"zTXt", b"eXIf"}
            while i < len(data):
                length = struct.unpack(">I", data[i:i + 4])[0]
                chunk_type = data[i + 4:i + 8]
                chunk_end = i + 12 + length
                if chunk_type not in STRIP_TYPES:
                    out.write(data[i:chunk_end])
                i = chunk_end
            return out.getvalue()
        except Exception:
            return data  # return original if stripping fails


class PDFMetadataStripper:
    """
    Strips /Info dictionary and XMP metadata stream from PDFs.
    Prevents injected prompts in PDF metadata from reaching LLM context.

    Usage:
        stripper = PDFMetadataStripper()
        clean_pdf = stripper.strip(pdf_bytes)
    """

    def strip(self, data: bytes) -> bytes:
        import re
        # Remove /Info dictionary references
        clean = re.sub(rb"/Info\s+\d+\s+\d+\s+R", b"", data)
        # Remove XMP metadata streams (between <xpacket begin and end>)
        clean = re.sub(
            rb"<\?xpacket begin.*?<\?xpacket end[^?]*\?>",
            b"", clean, flags=re.DOTALL
        )
        return clean
```

---

## Solution 3: FileSizeAndRateLimiter — Prevent DoS via Large Uploads

```python
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, Optional


@dataclass
class UploadPolicy:
    max_file_bytes: int = 10 * 1024 * 1024   # 10 MB
    max_files_per_minute: int = 20
    max_total_bytes_per_minute: int = 50 * 1024 * 1024  # 50 MB
    allowed_mimes: frozenset = frozenset({
        "image/jpeg", "image/png", "image/webp",
        "application/pdf", "text/plain",
    })


class UploadRateLimiter:
    """
    Enforces per-session file upload limits.
    Prevents DoS via large files or high-volume upload flooding.

    Usage:
        limiter = UploadRateLimiter(UploadPolicy(max_file_bytes=5_000_000))
        try:
            limiter.check(session_id="s1", file_size=2_000_000, mime="image/jpeg")
        except UploadRejected as exc:
            return error_response(str(exc))
    """

    def __init__(self, policy: Optional[UploadPolicy] = None):
        self._policy = policy or UploadPolicy()
        self._counts: Dict[str, list] = defaultdict(list)
        self._bytes: Dict[str, list] = defaultdict(list)

    def _prune(self, session_id: str):
        window = time.time() - 60.0
        self._counts[session_id] = [t for t in self._counts[session_id] if t > window]
        self._bytes[session_id] = [b for b in self._bytes[session_id] if b[0] > window]

    def check(self, session_id: str, file_size: int, mime: str):
        p = self._policy
        if mime not in p.allowed_mimes:
            raise UploadRejected(f"MIME '{mime}' not allowed")
        if file_size > p.max_file_bytes:
            raise UploadRejected(
                f"File size {file_size} exceeds limit {p.max_file_bytes}"
            )
        self._prune(session_id)
        if len(self._counts[session_id]) >= p.max_files_per_minute:
            raise UploadRejected("Upload rate limit exceeded")
        total_bytes = sum(b for _, b in self._bytes[session_id])
        if total_bytes + file_size > p.max_total_bytes_per_minute:
            raise UploadRejected("Upload byte budget exceeded")
        now = time.time()
        self._counts[session_id].append(now)
        self._bytes[session_id].append((now, file_size))


class UploadRejected(ValueError):
    pass
```

---

## Solution 4: MultimodalContentPipeline — Full Validation + Sanitisation

```python
import asyncio
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional


@dataclass
class ProcessedContent:
    safe_bytes: bytes
    mime_type: str
    original_size: int
    sanitised_size: int
    metadata_stripped: bool
    validation_passed: bool


class MultimodalContentPipeline:
    """
    End-to-end secure processing pipeline for multimodal agent inputs.
    Validates → sanitises → enforces limits → forwards to LLM tool.

    Usage:
        pipeline = MultimodalContentPipeline(
            allowed_mimes=frozenset({"image/jpeg", "image/png", "application/pdf"}),
            max_file_mb=10,
        )
        processed = await pipeline.process(
            data=uploaded_bytes,
            declared_mime="image/jpeg",
            session_id="s1",
        )
        result = await vision_tool.analyse(processed.safe_bytes)
    """

    def __init__(self,
                 allowed_mimes: Optional[frozenset] = None,
                 max_file_mb: int = 10):
        policy = UploadPolicy(
            max_file_bytes=max_file_mb * 1024 * 1024,
            allowed_mimes=allowed_mimes or frozenset({
                "image/jpeg", "image/png", "image/webp", "application/pdf",
            }),
        )
        self._validator = MagicBytesValidator(allowed_mimes=policy.allowed_mimes)
        self._limiter = UploadRateLimiter(policy)
        self._img_stripper = ImageMetadataStripper()
        self._pdf_stripper = PDFMetadataStripper()

    async def process(self, data: bytes, declared_mime: str,
                       session_id: str) -> ProcessedContent:
        # 1. Rate limit + size check
        self._limiter.check(session_id, len(data), declared_mime)
        # 2. Magic bytes validation
        result = self._validator.validate(data, declared_mime)
        if not result.valid:
            raise UploadRejected(f"Content validation failed: {result.reason}")
        # 3. Metadata stripping
        sanitised = data
        stripped = False
        if declared_mime == "image/jpeg":
            sanitised = self._img_stripper.strip_jpeg(data)
            stripped = True
        elif declared_mime == "image/png":
            sanitised = self._img_stripper.strip_png(data)
            stripped = True
        elif declared_mime == "application/pdf":
            sanitised = self._pdf_stripper.strip(data)
            stripped = True
        return ProcessedContent(
            safe_bytes=sanitised,
            mime_type=declared_mime,
            original_size=len(data),
            sanitised_size=len(sanitised),
            metadata_stripped=stripped,
            validation_passed=True,
        )
```

---

## Solution 5: SteganographyScanner — Detect Hidden Text in Images

```python
import re
from typing import List, Optional


SUSPICIOUS_PATTERNS = [
    rb"ignore previous instructions",
    rb"you are now",
    rb"disregard",
    rb"system prompt",
    rb"act as",
    rb"jailbreak",
    rb"[Ss][Yy][Ss][Tt][Ee][Mm]\s*:",
]


class SteganographyScanner:
    """
    Scans image and document bytes for hidden prompt injection text.
    Checks EXIF fields, comment blocks, and raw byte patterns.

    Usage:
        scanner = SteganographyScanner()
        findings = scanner.scan(jpeg_bytes)
        if findings:
            raise PromptInjectionAttempt(findings)
    """

    def scan(self, data: bytes) -> List[str]:
        findings = []
        # Check for suspicious text patterns in raw bytes
        for pattern in SUSPICIOUS_PATTERNS:
            matches = re.findall(pattern, data, re.IGNORECASE)
            if matches:
                findings.append(
                    f"Suspicious pattern found: {pattern.decode('utf-8', errors='replace')!r}"
                )
        # Check for high density of printable ASCII in unexpected regions
        if self._high_text_density(data):
            findings.append("Unexpectedly high printable ASCII density — possible steganographic text")
        return findings

    def _high_text_density(self, data: bytes,
                            window: int = 512,
                            threshold: float = 0.85) -> bool:
        """Check if any 512-byte window has > 85% printable ASCII."""
        for i in range(0, len(data) - window, window):
            chunk = data[i:i + window]
            printable = sum(0x20 <= b < 0x7f for b in chunk)
            if printable / window > threshold:
                return True
        return False


class PromptInjectionAttempt(ValueError):
    pass
```

---

## Solution 6: ContentNegotiationMiddleware — ASGI Layer for All File Inputs

```python
import json
from typing import Callable, Optional


class ContentNegotiationMiddleware:
    """
    ASGI middleware that intercepts multipart/form-data file uploads,
    validates each file through the full security pipeline before passing
    the request to the agent handler.

    Usage (FastAPI):
        app.add_middleware(
            ContentNegotiationMiddleware,
            pipeline=MultimodalContentPipeline(max_file_mb=10),
        )
    """

    def __init__(self, app, pipeline: Optional[MultimodalContentPipeline] = None):
        self._app = app
        self._pipeline = pipeline or MultimodalContentPipeline()
        self._scanner = SteganographyScanner()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        content_type = dict(scope.get("headers", [])).get(b"content-type", b"").decode()
        if "multipart/form-data" not in content_type:
            await self._app(scope, receive, send)
            return

        # Let the handler validate uploaded files; inject pipeline into request state
        scope.setdefault("state", {})["content_pipeline"] = self._pipeline
        scope["state"]["steg_scanner"] = self._scanner
        await self._app(scope, receive, send)
```

---

## Comparison

| Approach | MIME Validation | Polyglot Detection | Metadata Strip | Rate Limit | Steg Scan |
|---|---|---|---|---|---|
| **MagicBytesValidator** | Yes | Yes | No | No | No |
| **ImageMetadataStripper** | No | No | Yes | No | No |
| **UploadRateLimiter** | Yes (allowlist) | No | No | Yes | No |
| **MultimodalContentPipeline** | Yes | Yes | Yes | Yes | No |
| **SteganographyScanner** | No | No | No | No | Yes |
| **ContentNegotiationMiddleware** | Yes | Yes | Yes | Yes | Yes |

**Key insight**: validate magic bytes first — a declared MIME type is a client-controlled value and must never be trusted directly. Strip all metadata before forwarding to the LLM; even innocuous-looking EXIF fields can be abused for prompt injection. Apply steganography scanning only to content from untrusted sources, as it adds non-trivial overhead.
