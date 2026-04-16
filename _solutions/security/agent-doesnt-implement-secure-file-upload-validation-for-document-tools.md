---
title: "Agent Doesn't Implement Secure File Upload Validation for Document Tools"
description: "Agents with document ingestion tools that accept user-supplied files are vulnerable to malicious file uploads: path traversal, MIME spoofing, zip bombs, and macro-embedded documents. Implement multi-layer file validation to reject dangerous uploads before they reach storage or processing."
date: 2026-04-16
difficulty: intermediate
category: security
slug: agent-doesnt-implement-secure-file-upload-validation-for-document-tools
tags: [file-upload, validation, security, malware, mime-type, document-tools]
symptoms:
  - "Agent accepts any file extension and processes it without type verification"
  - "MIME type is taken from Content-Type header which the client controls"
  - "Zip file with path traversal names (../../etc/passwd) extracted without sanitization"
  - "100MB zip bomb decompresses to 10GB and crashes the agent process"
  - "PDF with embedded macros or JavaScript executed during text extraction"
---

## Why This Happens

Document tool implementations typically receive a file path or bytes and immediately pass them to a parser (PyMuPDF, python-docx, pandas). Attackers can upload files with deceptive extensions, forge MIME types, embed executable payloads, or craft decompression bombs. Validation must check magic bytes (not extension), enforce size limits before and after decompression, sanitize path components, and optionally scan with a malware signature database before any parsing occurs.

## Solution 1: Magic Byte MIME Validator

```python
import struct
from dataclasses import dataclass
from typing import Dict, List, Optional, Set

@dataclass
class FileSignature:
    mime_type: str
    magic_bytes: bytes
    offset: int = 0
    extensions: Set[str] = None

    def __post_init__(self):
        if self.extensions is None:
            self.extensions = set()

KNOWN_SIGNATURES: List[FileSignature] = [
    FileSignature("application/pdf",       b"%PDF",           extensions={".pdf"}),
    FileSignature("application/zip",       b"PK\x03\x04",     extensions={".zip", ".docx", ".xlsx", ".pptx"}),
    FileSignature("application/zip",       b"PK\x05\x06",     extensions={".zip"}),
    FileSignature("image/png",             b"\x89PNG\r\n\x1a\n", extensions={".png"}),
    FileSignature("image/jpeg",            b"\xff\xd8\xff",   extensions={".jpg", ".jpeg"}),
    FileSignature("image/gif",             b"GIF87a",         extensions={".gif"}),
    FileSignature("image/gif",             b"GIF89a",         extensions={".gif"}),
    FileSignature("application/msword",    b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", extensions={".doc", ".xls", ".ppt"}),
    FileSignature("text/plain",            b"",               extensions={".txt", ".md", ".csv"}),
]

ALLOWED_MIME_TYPES: Set[str] = {
    "application/pdf",
    "text/plain",
    "image/png",
    "image/jpeg",
    "image/gif",
    "application/zip",   # only for DOCX/XLSX/PPTX
}

DANGEROUS_EXTENSIONS: Set[str] = {
    ".exe", ".bat", ".cmd", ".sh", ".ps1", ".vbs", ".js",
    ".jar", ".class", ".py", ".php", ".rb", ".pl", ".dll",
    ".so", ".dylib", ".scr", ".pif", ".com", ".msi",
}

class MagicByteMIMEValidator:
    """
    Validates file type by reading magic bytes, not trusting Content-Type
    or file extension. Rejects dangerous file types.
    """

    def detect_mime(self, data: bytes) -> Optional[str]:
        for sig in KNOWN_SIGNATURES:
            if sig.magic_bytes and len(data) >= sig.offset + len(sig.magic_bytes):
                chunk = data[sig.offset:sig.offset + len(sig.magic_bytes)]
                if chunk == sig.magic_bytes:
                    return sig.mime_type
        # Fallback: check if data is valid UTF-8 text
        try:
            data[:1024].decode("utf-8")
            return "text/plain"
        except UnicodeDecodeError:
            return "application/octet-stream"

    def validate(self, filename: str, data: bytes, claimed_mime: Optional[str] = None) -> dict:
        import os
        ext = os.path.splitext(filename.lower())[1]
        detected_mime = self.detect_mime(data)

        issues = []
        if ext in DANGEROUS_EXTENSIONS:
            issues.append(f"Dangerous extension: {ext}")
        if detected_mime not in ALLOWED_MIME_TYPES:
            issues.append(f"Disallowed MIME type: {detected_mime}")
        if claimed_mime and claimed_mime != detected_mime:
            issues.append(f"MIME mismatch: claimed={claimed_mime}, detected={detected_mime}")

        return {
            "valid": len(issues) == 0,
            "detected_mime": detected_mime,
            "claimed_mime": claimed_mime,
            "extension": ext,
            "issues": issues,
        }
```

## Solution 2: Size and Decompression Bomb Guard

```python
import io
import zipfile
from dataclasses import dataclass
from typing import List

@dataclass
class SizeLimits:
    max_upload_bytes: int = 50 * 1024 * 1024          # 50 MB
    max_decompressed_bytes: int = 200 * 1024 * 1024   # 200 MB
    max_compression_ratio: float = 50.0               # >50x expansion = bomb
    max_files_in_archive: int = 100
    max_single_filename_length: int = 255

class DecompressionBombGuard:
    """
    Checks ZIP/archive files for decompression bombs before extraction.
    Rejects archives whose total uncompressed size exceeds limits.
    """

    def __init__(self, limits: SizeLimits = None):
        self._limits = limits or SizeLimits()

    def check_size(self, data: bytes) -> dict:
        if len(data) > self._limits.max_upload_bytes:
            return {
                "safe": False,
                "reason": f"Upload size {len(data):,} bytes exceeds limit {self._limits.max_upload_bytes:,}",
            }
        return {"safe": True}

    def check_zip(self, data: bytes) -> dict:
        """Inspect zip central directory without extracting."""
        try:
            buf = io.BytesIO(data)
            with zipfile.ZipFile(buf, "r") as zf:
                infos = zf.infolist()

                if len(infos) > self._limits.max_files_in_archive:
                    return {
                        "safe": False,
                        "reason": f"Archive contains {len(infos)} files (max {self._limits.max_files_in_archive})",
                    }

                total_uncompressed = sum(info.file_size for info in infos)
                if total_uncompressed > self._limits.max_decompressed_bytes:
                    return {
                        "safe": False,
                        "reason": f"Decompressed size {total_uncompressed:,} bytes exceeds limit",
                    }

                if len(data) > 0:
                    ratio = total_uncompressed / len(data)
                    if ratio > self._limits.max_compression_ratio:
                        return {
                            "safe": False,
                            "reason": f"Compression ratio {ratio:.1f}x exceeds max {self._limits.max_compression_ratio}x (possible zip bomb)",
                        }

                for info in infos:
                    if len(info.filename) > self._limits.max_single_filename_length:
                        return {"safe": False, "reason": f"Filename too long: {info.filename[:50]}..."}

                return {"safe": True, "file_count": len(infos), "total_uncompressed": total_uncompressed}

        except zipfile.BadZipFile as exc:
            return {"safe": False, "reason": f"Invalid ZIP file: {exc}"}
```

## Solution 3: Path Traversal Prevention for Extracted Files

```python
import os
import pathlib
import zipfile
import io
from typing import Dict, List

class SafeArchiveExtractor:
    """
    Extracts ZIP archives while preventing path traversal attacks.
    Rejects any entry whose resolved path escapes the destination directory.
    """

    DANGEROUS_NAME_PATTERNS = [
        "..", "//", "\x00",  # null bytes, path separators
    ]

    def _is_safe_path(self, base_dir: str, filename: str) -> bool:
        """Returns True if the file would land inside base_dir."""
        # Normalize and resolve
        target = os.path.realpath(os.path.join(base_dir, filename))
        base = os.path.realpath(base_dir)
        return target.startswith(base + os.sep) or target == base

    def _sanitize_filename(self, filename: str) -> str:
        """Strip path components, keeping only the final filename."""
        name = pathlib.PurePosixPath(filename).name
        # Remove null bytes and other control characters
        name = "".join(c for c in name if c.isprintable() and c not in "/\\:")
        return name or "unnamed_file"

    def extract_safely(
        self,
        zip_data: bytes,
        destination: str,
        allowed_extensions: set = None,
    ) -> Dict[str, str]:
        """
        Returns dict of {original_name: safe_extracted_path}.
        Raises ValueError on any path traversal attempt.
        """
        allowed = allowed_extensions or {".pdf", ".txt", ".docx", ".xlsx", ".png", ".jpg"}
        extracted = {}

        buf = io.BytesIO(zip_data)
        with zipfile.ZipFile(buf, "r") as zf:
            for info in zf.infolist():
                original_name = info.filename

                # Check for dangerous patterns
                for pattern in self.DANGEROUS_NAME_PATTERNS:
                    if pattern in original_name:
                        raise ValueError(f"Path traversal attempt: {original_name!r}")

                safe_name = self._sanitize_filename(original_name)
                ext = os.path.splitext(safe_name.lower())[1]
                if ext not in allowed:
                    continue  # skip disallowed file types silently

                dest_path = os.path.join(destination, safe_name)
                if not self._is_safe_path(destination, safe_name):
                    raise ValueError(f"Path traversal detected for: {original_name!r}")

                with zf.open(info) as src, open(dest_path, "wb") as dst:
                    dst.write(src.read())
                extracted[original_name] = dest_path

        return extracted
```

## Solution 4: Content-Level Document Safety Checker

```python
import re
from typing import List

class DocumentContentSafetyChecker:
    """
    Checks document content for embedded scripts, macros, and
    known malicious patterns after safe extraction.
    """

    PDF_JAVASCRIPT_PATTERNS = [
        rb"/JavaScript",
        rb"/JS\s",
        rb"/OpenAction",
        rb"/AA\s",       # Additional Action
        rb"/Launch",
        rb"/URI\s",      # hyperlink actions
        rb"eval\(",
    ]

    OFFICE_MACRO_INDICATORS = [
        b"vbaProject",
        b"VBA",
        b"AutoOpen",
        b"Document_Open",
        b"Workbook_Open",
        b"Sub ",
        b"Function ",
    ]

    def check_pdf(self, data: bytes) -> dict:
        issues = []
        for pattern in self.PDF_JAVASCRIPT_PATTERNS:
            if re.search(pattern, data, re.IGNORECASE):
                issues.append(f"PDF contains suspicious pattern: {pattern!r}")
        return {"safe": len(issues) == 0, "issues": issues}

    def check_office_doc(self, data: bytes) -> dict:
        """Check DOCX/XLSX (ZIP-based Office formats) for macro indicators."""
        import io, zipfile
        issues = []
        try:
            buf = io.BytesIO(data)
            with zipfile.ZipFile(buf, "r") as zf:
                names = zf.namelist()
                if "word/vbaProject.bin" in names or "xl/vbaProject.bin" in names:
                    issues.append("Office document contains VBA macro project")
                for name in names:
                    if name.endswith(".xml"):
                        content = zf.read(name)
                        for indicator in self.OFFICE_MACRO_INDICATORS:
                            if indicator in content:
                                issues.append(f"Macro indicator '{indicator!r}' found in {name}")
                                break
        except Exception as exc:
            issues.append(f"Could not inspect office document: {exc}")
        return {"safe": len(issues) == 0, "issues": issues}

    def check(self, mime_type: str, data: bytes) -> dict:
        if mime_type == "application/pdf":
            return self.check_pdf(data)
        if mime_type in ("application/zip",):
            return self.check_office_doc(data)
        return {"safe": True, "issues": []}
```

## Solution 5: Unified File Upload Validator

```python
import asyncio
import os
import hashlib
import time
from dataclasses import dataclass
from typing import Optional

@dataclass
class ValidationResult:
    accepted: bool
    filename: str
    detected_mime: str
    sha256: str
    size_bytes: int
    issues: list
    validated_at: float

class FileUploadValidator:
    """
    Unified entry point: runs all validation steps and returns
    a ValidationResult. Any failure rejects the upload.
    """

    def __init__(
        self,
        mime_validator: MagicByteMIMEValidator,
        bomb_guard: DecompressionBombGuard,
        content_checker: DocumentContentSafetyChecker,
    ):
        self._mime = mime_validator
        self._bomb = bomb_guard
        self._content = content_checker

    async def validate(
        self,
        filename: str,
        data: bytes,
        claimed_mime: Optional[str] = None,
    ) -> ValidationResult:
        issues = []
        sha256 = hashlib.sha256(data).hexdigest()

        # Step 1: Size check
        size_result = self._bomb.check_size(data)
        if not size_result["safe"]:
            issues.append(size_result["reason"])
            return ValidationResult(
                accepted=False, filename=filename, detected_mime="unknown",
                sha256=sha256, size_bytes=len(data), issues=issues,
                validated_at=time.time(),
            )

        # Step 2: MIME validation
        mime_result = self._mime.validate(filename, data, claimed_mime)
        issues.extend(mime_result["issues"])
        detected_mime = mime_result["detected_mime"]

        # Step 3: Archive bomb check
        if detected_mime == "application/zip":
            zip_result = self._bomb.check_zip(data)
            if not zip_result["safe"]:
                issues.append(zip_result["reason"])

        # Step 4: Content safety
        content_result = self._content.check(detected_mime, data)
        issues.extend(content_result["issues"])

        return ValidationResult(
            accepted=len(issues) == 0,
            filename=filename,
            detected_mime=detected_mime,
            sha256=sha256,
            size_bytes=len(data),
            issues=issues,
            validated_at=time.time(),
        )
```

## Solution 6: Upload Audit Logger and Rate Limiter

```python
import asyncio
import time
from collections import defaultdict
from dataclasses import dataclass, asdict
from typing import Dict, List
import json

@dataclass
class UploadEvent:
    user_id: str
    filename: str
    sha256: str
    size_bytes: int
    accepted: bool
    issues: list
    timestamp: float

class UploadAuditLogger:
    def __init__(self, sink):
        self._sink = sink
        # Rate limiting: {user_id: [timestamps]}
        self._upload_times: Dict[str, List[float]] = defaultdict(list)
        self._rate_limit = 20      # max uploads per window
        self._window_seconds = 3600  # per hour

    async def record(self, event: UploadEvent) -> None:
        await self._sink.append(json.dumps(asdict(event)))

    def check_rate_limit(self, user_id: str) -> bool:
        """Returns True if user is within rate limit."""
        now = time.time()
        cutoff = now - self._window_seconds
        times = [t for t in self._upload_times[user_id] if t > cutoff]
        self._upload_times[user_id] = times
        if len(times) >= self._rate_limit:
            return False
        self._upload_times[user_id].append(now)
        return True

    async def rejected_count(self, user_id: str, window_hours: float = 24.0) -> int:
        # Query from sink for repeated rejections (potential attack pattern)
        cutoff = time.time() - window_hours * 3600
        events = await self._sink.query(user_id=user_id, since=cutoff, accepted=False)
        return len(events)
```

## Comparison

| Approach | Threat Mitigated | False Positive Risk | Performance Impact |
|---|---|---|---|
| MagicByteMIMEValidator | MIME spoofing, dangerous extensions | Low | Negligible (header only) |
| DecompressionBombGuard | Zip bombs, oversized archives | Low | Low (metadata scan) |
| SafeArchiveExtractor | Path traversal | None (by design) | Low |
| DocumentContentSafetyChecker | Macros, embedded scripts | Medium (regex scan) | Low–Medium |
| FileUploadValidator | All of the above | Low | Low combined |
| UploadAuditLogger | Abuse detection, rate limiting | None | Negligible |

**Best for production**: Run all steps in `FileUploadValidator` in sequence. Magic byte validation and size checks are cheap and should always run. Archive extraction must always use `SafeArchiveExtractor`. Content safety scanning is optional for low-risk workloads but required for any agent that processes user-supplied documents. Rate limit uploads per user with `UploadAuditLogger`.
