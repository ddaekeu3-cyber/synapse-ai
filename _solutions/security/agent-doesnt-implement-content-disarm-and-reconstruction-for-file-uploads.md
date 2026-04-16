---
title: "Agent Doesn't Implement Content Disarm and Reconstruction for File Uploads"
description: "Agents that process user-uploaded documents without sanitization pass raw files — including embedded macros, scripts, and active content — directly to processing pipelines. Implement Content Disarm and Reconstruction (CDR) to strip active content from uploaded files before any parsing, extraction, or LLM injection, while preserving usable document content."
date: 2026-04-16
difficulty: advanced
category: security
slug: agent-doesnt-implement-content-disarm-and-reconstruction-for-file-uploads
tags: [cdr, file-upload, document-security, macro-stripping, active-content, security]
symptoms:
  - "PDF with embedded JavaScript executes during text extraction in the agent pipeline"
  - "Word document with macro payload bypasses file type validation and runs on upload"
  - "SVG file with embedded script injected into the LLM context causes prompt injection"
  - "Agent processes XLSX with DDE formulas that trigger external data fetches"
  - "No sanitization between user file upload and document-to-text extraction"
---

## Why This Happens

File formats designed for rich documents (PDF, DOCX, XLSX, SVG) support active content: JavaScript in PDFs, VBA macros in Office files, DDE formulas in spreadsheets, and inline scripts in SVG. When agents extract text from these files to build LLM context, active content can execute in the parsing library, inject malicious content into the extracted text, or cause SSRF via external resource loads. CDR removes all active content before processing while preserving the document's text, structure, and safe formatting.

## Solution 1: File Type Validator and Allow-List Gate

```python
import hashlib
import struct
from dataclasses import dataclass
from typing import Optional

ALLOWED_MIME_TYPES = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "text/plain",
    "text/csv",
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
}

# Magic bytes for file type verification
FILE_MAGIC = {
    b"\x25\x50\x44\x46": "application/pdf",           # %PDF
    b"\x50\x4b\x03\x04": "application/zip",            # ZIP (OOXML base)
    b"\xff\xd8\xff": "image/jpeg",
    b"\x89PNG": "image/png",
    b"GIF8": "image/gif",
    b"RIFF": "image/webp",
}

OOXML_CONTENT_TYPES = {
    "word/": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xl/": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "ppt/": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}

@dataclass
class FileValidationResult:
    allowed: bool
    detected_mime: Optional[str]
    claimed_mime: Optional[str]
    mime_mismatch: bool
    file_hash: str
    reason: str

class FileTypeValidator:
    """
    Validates uploaded files by magic bytes, not file extension or claimed MIME.
    Rejects files whose actual type differs from claimed type.
    """

    MAX_FILE_SIZE = 50 * 1024 * 1024   # 50MB

    def detect_mime(self, data: bytes) -> Optional[str]:
        for magic, mime in FILE_MAGIC.items():
            if data[:len(magic)] == magic:
                if mime == "application/zip":
                    # Distinguish OOXML types by ZIP contents listing
                    return self._detect_ooxml(data)
                return mime
        # Check for plain text (UTF-8)
        try:
            data[:1024].decode("utf-8")
            return "text/plain"
        except UnicodeDecodeError:
            return None

    def _detect_ooxml(self, data: bytes) -> str:
        """Inspect ZIP central directory for OOXML type hints."""
        try:
            import zipfile, io
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                names = z.namelist()
            for prefix, mime in OOXML_CONTENT_TYPES.items():
                if any(n.startswith(prefix) for n in names):
                    return mime
        except Exception:
            pass
        return "application/zip"

    def validate(self, data: bytes, claimed_mime: Optional[str] = None) -> FileValidationResult:
        file_hash = hashlib.sha256(data).hexdigest()

        if len(data) > self.MAX_FILE_SIZE:
            return FileValidationResult(
                allowed=False, detected_mime=None, claimed_mime=claimed_mime,
                mime_mismatch=False, file_hash=file_hash,
                reason=f"file_too_large:{len(data)}_bytes",
            )

        detected = self.detect_mime(data)
        mismatch = claimed_mime is not None and detected != claimed_mime

        if detected not in ALLOWED_MIME_TYPES:
            return FileValidationResult(
                allowed=False, detected_mime=detected, claimed_mime=claimed_mime,
                mime_mismatch=mismatch, file_hash=file_hash,
                reason=f"disallowed_type:{detected}",
            )

        return FileValidationResult(
            allowed=True, detected_mime=detected, claimed_mime=claimed_mime,
            mime_mismatch=mismatch, file_hash=file_hash, reason="ok",
        )
```

## Solution 2: PDF Active Content Stripper

```python
import re
from dataclasses import dataclass
from typing import List, Optional

@dataclass
class PDFSanitizationResult:
    sanitized_data: bytes
    removed_elements: List[str]
    original_size: int
    sanitized_size: int

class PDFActiveContentStripper:
    """
    Strips JavaScript, embedded files, launch actions, URI actions,
    and form submission actions from PDF byte streams.
    Works on raw PDF bytes without executing any content.
    """

    # Patterns that indicate active content in PDF
    DANGEROUS_PATTERNS = [
        (rb"/JavaScript\s", "JavaScript"),
        (rb"/JS\s", "JS_shorthand"),
        (rb"/Launch\s", "Launch_action"),
        (rb"/SubmitForm\s", "SubmitForm_action"),
        (rb"/ImportData\s", "ImportData_action"),
        (rb"/EmbeddedFile\s", "EmbeddedFile"),
        (rb"/RichMedia\s", "RichMedia"),
        (rb"/OpenAction\s*/JavaScript", "OpenAction_JS"),
        (rb"app\.alert", "app_alert_JS"),
        (rb"this\.submitForm", "submitForm_JS"),
    ]

    def strip(self, pdf_bytes: bytes) -> PDFSanitizationResult:
        removed = []
        data = pdf_bytes

        for pattern, label in self.DANGEROUS_PATTERNS:
            matches = re.findall(pattern, data, re.IGNORECASE)
            if matches:
                removed.append(f"{label}({len(matches)})")
                # Replace with a benign comment marker
                data = re.sub(pattern, b"/Sanitized_" + label.encode(), data, flags=re.IGNORECASE)

        # Remove JavaScript streams (between stream...endstream containing JS keywords)
        js_stream_pattern = rb"stream\r?\n.*?(?:JavaScript|app\.alert|this\.).*?endstream"
        js_streams = re.findall(js_stream_pattern, data, re.DOTALL | re.IGNORECASE)
        if js_streams:
            removed.append(f"JS_streams({len(js_streams)})")
            data = re.sub(
                js_stream_pattern,
                b"stream\n% sanitized\nendstream",
                data,
                flags=re.DOTALL | re.IGNORECASE,
            )

        return PDFSanitizationResult(
            sanitized_data=data,
            removed_elements=removed,
            original_size=len(pdf_bytes),
            sanitized_size=len(data),
        )
```

## Solution 3: Office Document Macro Remover

```python
import io
import zipfile
from dataclasses import dataclass
from typing import List, Optional

@dataclass
class OfficeSanitizationResult:
    sanitized_data: Optional[bytes]
    removed_parts: List[str]
    original_size: int
    error: Optional[str] = None

class OfficeDocumentMacroRemover:
    """
    Removes VBA macros, DDE formulas, and external data connections
    from OOXML (DOCX, XLSX, PPTX) files by rewriting the ZIP archive
    without macro-containing parts.
    """

    MACRO_PART_PATTERNS = [
        "vbaProject.bin",
        "vbaProjectSignature.bin",
        "xl/macrosheets/",
        "word/vbaProject",
        "xl/xlmacrosheets/",
    ]

    DANGEROUS_CONTENT_TYPES = [
        "application/vnd.ms-office.activeX",
        "application/vnd.ms-powerpoint.addin.macroEnabled",
        "application/vnd.ms-excel.sheet.macroEnabled",
        "application/vnd.ms-word.document.macroEnabled",
    ]

    def sanitize(self, ooxml_bytes: bytes) -> OfficeSanitizationResult:
        removed = []
        try:
            with zipfile.ZipFile(io.BytesIO(ooxml_bytes)) as zin:
                names = zin.namelist()
                out_buf = io.BytesIO()
                with zipfile.ZipFile(out_buf, "w", zipfile.ZIP_DEFLATED) as zout:
                    for name in names:
                        # Skip macro parts
                        if any(pat in name for pat in self.MACRO_PART_PATTERNS):
                            removed.append(f"macro_part:{name}")
                            continue

                        data = zin.read(name)

                        # Sanitize content type registration
                        if name == "[Content_Types].xml":
                            data = self._strip_macro_content_types(data, removed)

                        # Strip DDE from shared strings (XLSX)
                        if name == "xl/sharedStrings.xml":
                            data = self._strip_dde_formulas(data, removed)

                        # Strip external data connections
                        if "connections" in name.lower():
                            removed.append(f"connections:{name}")
                            continue

                        zout.writestr(name, data)

                return OfficeSanitizationResult(
                    sanitized_data=out_buf.getvalue(),
                    removed_parts=removed,
                    original_size=len(ooxml_bytes),
                )
        except Exception as exc:
            return OfficeSanitizationResult(
                sanitized_data=None,
                removed_parts=[],
                original_size=len(ooxml_bytes),
                error=str(exc),
            )

    def _strip_macro_content_types(self, xml_data: bytes, removed: List[str]) -> bytes:
        import re
        for ct in self.DANGEROUS_CONTENT_TYPES:
            pattern = rb'<Override[^>]+ContentType="[^"]*' + re.escape(ct.encode()) + rb'[^"]*"[^/]*/>'
            matches = re.findall(pattern, xml_data, re.IGNORECASE)
            if matches:
                removed.append(f"content_type:{ct}")
                xml_data = re.sub(pattern, b"", xml_data, flags=re.IGNORECASE)
        return xml_data

    def _strip_dde_formulas(self, xml_data: bytes, removed: List[str]) -> bytes:
        import re
        # DDE references look like =DDE("app","topic","item") in shared strings
        dde_pattern = rb'=DDE\([^)]+\)'
        matches = re.findall(dde_pattern, xml_data, re.IGNORECASE)
        if matches:
            removed.append(f"dde_formulas({len(matches)})")
            xml_data = re.sub(dde_pattern, b"[DDE_REMOVED]", xml_data, flags=re.IGNORECASE)
        return xml_data
```

## Solution 4: SVG Script Sanitizer

```python
import re
from dataclasses import dataclass
from typing import List

@dataclass
class SVGSanitizationResult:
    sanitized_xml: str
    removed_elements: List[str]
    original_size: int

class SVGScriptSanitizer:
    """
    Strips script elements, event handlers, and external resource loads
    from SVG before injecting SVG-derived text into LLM context.
    SVGs are XML and can carry arbitrary JS through multiple vectors.
    """

    DANGEROUS_ELEMENTS = [
        r"<script[^>]*>.*?</script>",
        r"<script[^/]*/?>",
        r"<use[^>]+href\s*=\s*['\"]https?://[^'\"]+['\"][^>]*/?>",
        r"<image[^>]+href\s*=\s*['\"]https?://[^'\"]+['\"][^>]*/?>",
        r"<foreignObject[^>]*>.*?</foreignObject>",
    ]

    # Event handler attributes that can carry JavaScript
    EVENT_ATTRIBUTES = [
        r'\s+on\w+\s*=\s*["\'][^"\']*["\']',
        r'\s+href\s*=\s*["\']javascript:[^"\']*["\']',
        r'\s+xlink:href\s*=\s*["\']javascript:[^"\']*["\']',
    ]

    def sanitize(self, svg_content: str) -> SVGSanitizationResult:
        removed = []
        data = svg_content

        for pattern in self.DANGEROUS_ELEMENTS:
            matches = re.findall(pattern, data, re.DOTALL | re.IGNORECASE)
            if matches:
                removed.append(f"element({len(matches)})")
                data = re.sub(pattern, "", data, flags=re.DOTALL | re.IGNORECASE)

        for pattern in self.EVENT_ATTRIBUTES:
            matches = re.findall(pattern, data, re.IGNORECASE)
            if matches:
                removed.append(f"event_attr({len(matches)})")
                data = re.sub(pattern, "", data, flags=re.IGNORECASE)

        return SVGSanitizationResult(
            sanitized_xml=data,
            removed_elements=removed,
            original_size=len(svg_content),
        )
```

## Solution 5: CDR Pipeline Orchestrator

```python
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

@dataclass
class CDRResult:
    file_hash: str
    original_mime: str
    sanitized_data: Optional[bytes]
    removed_elements: List[str]
    allowed: bool
    processing_ms: float
    error: Optional[str] = None

class CDRPipeline:
    """
    Orchestrates file validation and sanitization into a single entry point.
    All uploaded files pass through CDR before any content extraction.
    """

    def __init__(
        self,
        validator: FileTypeValidator,
        pdf_stripper: PDFActiveContentStripper,
        office_remover: OfficeDocumentMacroRemover,
        svg_sanitizer: SVGScriptSanitizer,
    ):
        self._validator = validator
        self._pdf = pdf_stripper
        self._office = office_remover
        self._svg = svg_sanitizer

    def process(self, data: bytes, claimed_mime: Optional[str] = None) -> CDRResult:
        t0 = time.monotonic()

        validation = self._validator.validate(data, claimed_mime)
        if not validation.allowed:
            return CDRResult(
                file_hash=validation.file_hash,
                original_mime=validation.detected_mime or "unknown",
                sanitized_data=None,
                removed_elements=[],
                allowed=False,
                processing_ms=round((time.monotonic() - t0) * 1000, 2),
                error=validation.reason,
            )

        mime = validation.detected_mime
        removed: List[str] = []

        if mime == "application/pdf":
            result = self._pdf.strip(data)
            sanitized = result.sanitized_data
            removed = result.removed_elements

        elif mime in (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ):
            result = self._office.sanitize(data)
            sanitized = result.sanitized_data or data
            removed = result.removed_parts
            if result.error:
                return CDRResult(
                    file_hash=validation.file_hash,
                    original_mime=mime,
                    sanitized_data=None,
                    removed_elements=[],
                    allowed=False,
                    processing_ms=round((time.monotonic() - t0) * 1000, 2),
                    error=f"office_sanitization_failed:{result.error}",
                )
        else:
            sanitized = data   # text/plain, images — pass through as-is

        return CDRResult(
            file_hash=validation.file_hash,
            original_mime=mime,
            sanitized_data=sanitized,
            removed_elements=removed,
            allowed=True,
            processing_ms=round((time.monotonic() - t0) * 1000, 2),
        )
```

## Solution 6: CDR Audit Logger

```python
import json
import time
from dataclasses import asdict, dataclass, field
from typing import List

@dataclass
class CDRAuditEntry:
    file_hash: str
    original_mime: str
    allowed: bool
    removed_count: int
    removed_summary: str
    processing_ms: float
    user_id: str
    session_id: str
    timestamp: float = field(default_factory=time.time)

class CDRAuditLogger:
    def __init__(self, storage_backend=None):
        self._backend = storage_backend
        self._entries: List[CDRAuditEntry] = []

    def log(self, result: CDRResult, user_id: str, session_id: str) -> None:
        entry = CDRAuditEntry(
            file_hash=result.file_hash,
            original_mime=result.original_mime,
            allowed=result.allowed,
            removed_count=len(result.removed_elements),
            removed_summary=", ".join(result.removed_elements[:5]) if result.removed_elements else "none",
            processing_ms=result.processing_ms,
            user_id=user_id,
            session_id=session_id,
        )
        self._entries.append(entry)
        record = {
            "file_hash": entry.file_hash,
            "mime": entry.original_mime,
            "allowed": entry.allowed,
            "removed": entry.removed_summary,
            "user": user_id,
            "session": session_id,
            "ms": entry.processing_ms,
            "ts": entry.timestamp,
        }
        print(f"[cdr_audit] {json.dumps(record)}")
        if self._backend:
            self._backend.append(record)

    def threat_summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [e for e in self._entries if e.timestamp >= cutoff]
        blocked = [e for e in recent if not e.allowed]
        sanitized = [e for e in recent if e.allowed and e.removed_count > 0]
        return {
            "window_seconds": window_seconds,
            "total_files": len(recent),
            "blocked": len(blocked),
            "sanitized": len(sanitized),
            "clean": len(recent) - len(blocked) - len(sanitized),
            "unique_users_with_threats": len({e.user_id for e in blocked + sanitized}),
        }
```

## Comparison

| Approach | Covers PDF | Covers OOXML | Covers SVG | Blocks Unknown | Audit Trail |
|---|---|---|---|---|---|
| FileTypeValidator | Magic check only | Magic check only | Magic check only | Yes | No |
| PDFActiveContentStripper | Yes | No | No | No | No |
| OfficeDocumentMacroRemover | No | Yes | No | No | No |
| SVGScriptSanitizer | No | No | Yes | No | No |
| CDRPipeline | Yes | Yes | Partial | Yes | No |
| CDRAuditLogger | N/A | N/A | N/A | N/A | Yes |

**Best for production**: Route all file uploads through `CDRPipeline` before any content extraction or LLM injection. Reject unknown MIME types at the `FileTypeValidator` gate — never pass unrecognized formats downstream. Apply type-specific sanitizers (PDF, OOXML, SVG) to strip active content while preserving text. Log every file through `CDRAuditLogger` for SOC review and to detect users repeatedly submitting files with active content. Never trust file extensions — always validate by magic bytes.
