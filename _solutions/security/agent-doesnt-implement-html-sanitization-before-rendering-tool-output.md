---
title: "Agent Doesn't Implement HTML Sanitization Before Rendering Tool Output"
description: "AI agents that pass raw tool output directly into HTML rendering surfaces — chat UIs, dashboards, email templates — allow tool responses containing script tags, event handlers, or malicious iframes to execute in the user's browser. HTML sanitization strips disallowed tags and attributes before rendering, preventing stored and reflected XSS attacks from tool-sourced content."
date: 2025-02-18
difficulty: intermediate
category: security
slug: agent-doesnt-implement-html-sanitization-before-rendering-tool-output
tags:
  - xss
  - html-sanitization
  - security
  - output-rendering
  - tool-output
  - content-security
  - injection
symptoms:
  - "Tool output containing <script> tags is rendered directly in the chat UI"
  - "A web_search result with JavaScript event handlers executes when displayed"
  - "No allowlist of permitted HTML tags applied to tool responses before rendering"
  - "Markdown rendered to HTML without stripping dangerous attributes like onerror"
  - "Agent email templates include unescaped tool output, enabling HTML injection"
---

## Problem

Tool outputs are untrusted content from external sources: web search results, database records, API responses. When an agent renders this content in HTML contexts — chat UI, email, generated reports — any `<script>`, `onload=`, `javascript:` href, or `<iframe>` in the tool response becomes executable in the user's browser. HTML sanitization applies an allowlist of safe tags and attributes, strips or escapes everything else, and neutralizes dangerous patterns before the content reaches the renderer. It is the last line of defense against XSS attacks that exploit the implicit trust users place in agent-generated content.

---

## Solution 1: HTMLAllowlistSanitizer — Strip Disallowed Tags and Attributes

```python
import html
import logging
import re
from typing import Dict, Optional, Set

logger = logging.getLogger(__name__)


class HTMLAllowlistSanitizer:
    """
    Sanitizes HTML by parsing tag structure with a simple regex-based
    allowlist approach. For production use, replace the regex parser
    with a proper HTML parser (bleach, lxml, or html-sanitizer).
    This implementation illustrates the allowlist pattern for environments
    where installing additional libraries is constrained.

    Usage:
        sanitizer = HTMLAllowlistSanitizer()
        safe_html = sanitizer.sanitize(tool_output_html)
        render_in_ui(safe_html)
    """

    # Tags that are safe and their allowed attributes
    ALLOWED_TAGS: Dict[str, Set[str]] = {
        "p": set(),
        "br": set(),
        "strong": set(),
        "em": set(),
        "b": set(),
        "i": set(),
        "u": set(),
        "ul": set(),
        "ol": set(),
        "li": set(),
        "h1": set(), "h2": set(), "h3": set(), "h4": set(),
        "blockquote": set(),
        "code": set(),
        "pre": set(),
        "span": {"class"},
        "div": {"class"},
        "table": set(),
        "thead": set(), "tbody": set(),
        "tr": set(),
        "th": {"scope"},
        "td": {"colspan", "rowspan"},
        "a": {"href", "title"},   # href sanitized further below
        "img": {"src", "alt", "width", "height"},  # src sanitized further
    }

    # Patterns that must never appear in attribute values
    DANGEROUS_PATTERNS = re.compile(
        r'javascript:|data:text/html|vbscript:|'
        r'on\w+\s*=',  # inline event handlers
        re.IGNORECASE,
    )

    # Match any HTML tag
    TAG_RE = re.compile(r'<(/?)(\w+)([^>]*)>', re.DOTALL)
    ATTR_RE = re.compile(r'(\w[\w-]*)(?:\s*=\s*(?:"([^"]*?)"|\'([^\']*?)\'|(\S+)))?')

    def sanitize(self, html_content: str) -> str:
        if not html_content:
            return ""
        result = self.TAG_RE.sub(self._filter_tag, html_content)
        return result

    def _filter_tag(self, match: re.Match) -> str:
        closing = match.group(1)
        tag = match.group(2).lower()
        attrs_str = match.group(3)

        if tag not in self.ALLOWED_TAGS:
            logger.debug("html_tag_stripped tag=%s", tag)
            return ""

        if closing:
            return f"</{tag}>"

        allowed_attrs = self.ALLOWED_TAGS[tag]
        safe_attrs = self._filter_attrs(attrs_str, allowed_attrs, tag)
        return f"<{tag}{safe_attrs}>"

    def _filter_attrs(self, attrs_str: str,
                       allowed: Set[str], tag: str) -> str:
        if not attrs_str.strip():
            return ""
        safe_parts = []
        for m in self.ATTR_RE.finditer(attrs_str):
            attr_name = m.group(1).lower()
            value = m.group(2) or m.group(3) or m.group(4) or ""

            if attr_name not in allowed:
                continue
            if self.DANGEROUS_PATTERNS.search(value):
                logger.warning(
                    "html_attr_dangerous_value attr=%s value=%s",
                    attr_name, value[:30],
                )
                continue
            # Validate href/src: only allow http/https/relative
            if attr_name in ("href", "src"):
                value = self._safe_url(value)
                if value is None:
                    continue
            safe_parts.append(f'{attr_name}="{html.escape(value, quote=True)}"')

        return (" " + " ".join(safe_parts)) if safe_parts else ""

    def _safe_url(self, url: str) -> Optional[str]:
        url = url.strip()
        if url.startswith(("/", "./", "../", "http://", "https://")):
            return url
        logger.warning("html_url_stripped url=%s", url[:40])
        return None

    def escape_text(self, text: str) -> str:
        """Escape plain text for safe insertion into HTML context."""
        return html.escape(text, quote=True)
```

---

## Solution 2: MarkdownSafeRenderer — Sanitize Markdown-to-HTML Output

```python
import html
import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)


class MarkdownSafeRenderer:
    """
    Renders Markdown to HTML and sanitizes the result. Markdown renderers
    (mistune, markdown2, commonmark) often pass raw HTML through unchanged
    when users embed it — this wrapper sanitizes the output before display.

    Usage:
        renderer = MarkdownSafeRenderer(sanitizer)
        safe_html = renderer.render(markdown_tool_output)
    """

    def __init__(self, sanitizer: Optional[HTMLAllowlistSanitizer] = None):
        self._sanitizer = sanitizer or HTMLAllowlistSanitizer()

    def render(self, markdown_text: str) -> str:
        """Convert markdown to safe HTML."""
        # Try using a real markdown library if available
        try:
            import mistune
            raw_html = mistune.html(markdown_text)
        except ImportError:
            try:
                import markdown
                raw_html = markdown.markdown(markdown_text, extensions=["tables"])
            except ImportError:
                # Minimal fallback: escape everything
                raw_html = self._minimal_markdown(markdown_text)

        safe = self._sanitizer.sanitize(raw_html)
        return safe

    def _minimal_markdown(self, text: str) -> str:
        """Minimal safe conversion without a library."""
        # Escape all HTML first
        escaped = html.escape(text)
        # Convert **bold**
        escaped = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', escaped)
        # Convert *italic*
        escaped = re.sub(r'\*(.+?)\*', r'<em>\1</em>', escaped)
        # Convert `code`
        escaped = re.sub(r'`([^`]+)`', r'<code>\1</code>', escaped)
        # Convert newlines to <br>
        escaped = escaped.replace('\n\n', '</p><p>').replace('\n', '<br>')
        return f"<p>{escaped}</p>"

    def render_to_text(self, markdown_text: str) -> str:
        """Strip all markup and return plain text (for email subjects, etc.)."""
        rendered = self.render(markdown_text)
        return re.sub(r'<[^>]+>', '', rendered).strip()
```

---

## Solution 3: ToolOutputRenderer — Context-Aware Output Sanitization

```python
import html
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


class ToolOutputRenderer:
    """
    Renders tool output safely based on the declared output type and
    the rendering context (html, plain_text, json, email). Applies the
    appropriate sanitization for each context to prevent injection.

    Usage:
        renderer = ToolOutputRenderer(sanitizer, markdown_renderer)
        safe = renderer.render(
            content=tool_result,
            output_type="markdown",
            context="html",
        )
    """

    def __init__(self,
                  sanitizer: Optional[HTMLAllowlistSanitizer] = None,
                  md_renderer: Optional[MarkdownSafeRenderer] = None):
        self._sanitizer = sanitizer or HTMLAllowlistSanitizer()
        self._md = md_renderer or MarkdownSafeRenderer(self._sanitizer)

    def render(self, content: Any,
                output_type: str = "text",
                context: str = "html") -> str:
        """
        Render content safely.
        output_type: "text", "markdown", "html", "json"
        context: "html", "plain_text", "email", "attribute"
        """
        text = self._to_string(content, output_type)

        if context == "html":
            return self._render_html(text, output_type)
        if context == "plain_text":
            return self._strip_to_plain(text)
        if context == "email":
            return self._render_email(text, output_type)
        if context == "attribute":
            # For insertion into an HTML attribute value
            return html.escape(self._strip_to_plain(text), quote=True)
        return html.escape(text)

    def _to_string(self, content: Any, output_type: str) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, (dict, list)):
            return json.dumps(content, indent=2, ensure_ascii=False)
        return str(content)

    def _render_html(self, text: str, output_type: str) -> str:
        if output_type == "markdown":
            return self._md.render(text)
        if output_type == "html":
            return self._sanitizer.sanitize(text)
        # Plain text — escape and preserve whitespace
        escaped = html.escape(text)
        return f"<pre>{escaped}</pre>"

    def _strip_to_plain(self, text: str) -> str:
        import re
        return re.sub(r'<[^>]+>', '', text).strip()

    def _render_email(self, text: str, output_type: str) -> str:
        # Email HTML context: more restrictive — no scripts, no forms
        safe = self._render_html(text, output_type)
        # Additionally strip any remaining form/script tags
        import re
        safe = re.sub(r'<(script|form|input|button|select|textarea)[^>]*>.*?</\1>',
                       '', safe, flags=re.DOTALL | re.IGNORECASE)
        return safe
```

---

## Solution 4: ContentSecurityPolicyHelper — Add CSP Headers for Defense in Depth

```python
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class CSPPolicy:
    default_src: List[str] = field(default_factory=lambda: ["'self'"])
    script_src: List[str] = field(default_factory=lambda: ["'self'"])
    style_src: List[str] = field(default_factory=lambda: ["'self'", "'unsafe-inline'"])
    img_src: List[str] = field(default_factory=lambda: ["'self'", "data:", "https:"])
    connect_src: List[str] = field(default_factory=lambda: ["'self'"])
    frame_ancestors: List[str] = field(default_factory=lambda: ["'none'"])
    form_action: List[str] = field(default_factory=lambda: ["'self'"])
    base_uri: List[str] = field(default_factory=lambda: ["'self'"])


class ContentSecurityPolicyHelper:
    """
    Generates Content-Security-Policy headers to complement HTML sanitization.
    Even if a sanitization bypass is discovered, a strict CSP prevents
    injected scripts from executing in the browser.

    Usage:
        csp = ContentSecurityPolicyHelper()
        headers = csp.headers_for("chat_ui")
        response.headers.update(headers)
    """

    PROFILES: Dict[str, CSPPolicy] = {
        "strict": CSPPolicy(
            script_src=["'self'"],
            style_src=["'self'"],
        ),
        "chat_ui": CSPPolicy(
            script_src=["'self'", "'nonce-{nonce}'"],
            style_src=["'self'", "'unsafe-inline'"],
            img_src=["'self'", "data:", "https:"],
        ),
        "report_only": CSPPolicy(
            script_src=["'self'", "'unsafe-inline'"],
        ),
    }

    def headers_for(self, profile: str = "strict",
                     nonce: Optional[str] = None) -> Dict[str, str]:
        policy = self.PROFILES.get(profile, self.PROFILES["strict"])
        directives = self._build(policy, nonce)
        return {
            "Content-Security-Policy": "; ".join(directives),
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "strict-origin-when-cross-origin",
        }

    def _build(self, policy: CSPPolicy,
                nonce: Optional[str] = None) -> List[str]:
        parts = []
        field_map = {
            "default-src": policy.default_src,
            "script-src": [
                s.replace("{nonce}", nonce) if nonce else s
                for s in policy.script_src
                if nonce or "{nonce}" not in s
            ],
            "style-src": policy.style_src,
            "img-src": policy.img_src,
            "connect-src": policy.connect_src,
            "frame-ancestors": policy.frame_ancestors,
            "form-action": policy.form_action,
            "base-uri": policy.base_uri,
        }
        for directive, sources in field_map.items():
            if sources:
                parts.append(f"{directive} {' '.join(sources)}")
        return parts
```

---

## Solution 5: SanitizationAuditLogger — Track Sanitization Events

```python
import logging
import re
import time
from collections import defaultdict
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


class SanitizationAuditLogger:
    """
    Wraps a sanitizer and records every stripping/modification event.
    High rates of sanitization indicate that tool sources are returning
    malicious or poorly-formed HTML that warrants investigation.

    Usage:
        audit = SanitizationAuditLogger(base_sanitizer)
        safe = audit.sanitize(raw_tool_output, source="web_search")
        if audit.dangerous_rate(window_s=300) > 0.05:
            alert_security_team()
    """

    def __init__(self, sanitizer: HTMLAllowlistSanitizer):
        self._sanitizer = sanitizer
        self._events: List[Dict[str, Any]] = []
        self._total = 0
        self._dangerous = 0

    def sanitize(self, content: str, source: str = "") -> str:
        self._total += 1
        original_len = len(content)
        safe = self._sanitizer.sanitize(content)
        safe_len = len(safe)

        # Detect if significant content was stripped
        stripped_fraction = 1 - (safe_len / max(original_len, 1))
        has_dangerous = bool(
            re.search(r'<script|javascript:|on\w+\s*=', content, re.IGNORECASE)
        )

        if has_dangerous:
            self._dangerous += 1
            logger.warning(
                "sanitization_dangerous_input source=%s stripped_pct=%.0f",
                source, stripped_fraction * 100,
            )
            self._events.append({
                "source": source,
                "original_len": original_len,
                "safe_len": safe_len,
                "stripped_fraction": round(stripped_fraction, 3),
                "dangerous": True,
                "ts": time.time(),
            })
        return safe

    def dangerous_rate(self, window_s: float = 300.0) -> float:
        cutoff = time.time() - window_s
        recent_total = sum(1 for e in self._events if e["ts"] >= cutoff) + (
            self._total - len(self._events)
        )
        recent_dangerous = sum(1 for e in self._events
                                if e["ts"] >= cutoff and e["dangerous"])
        return recent_dangerous / max(recent_total, 1)

    def top_dangerous_sources(self, n: int = 5) -> List[str]:
        counts: Dict[str, int] = defaultdict(int)
        for e in self._events:
            if e["dangerous"]:
                counts[e["source"]] += 1
        return [s for s, _ in sorted(counts.items(), key=lambda x: -x[1])[:n]]

    def stats(self) -> Dict[str, Any]:
        return {
            "total_sanitized": self._total,
            "dangerous_count": self._dangerous,
            "dangerous_rate": round(self.dangerous_rate(), 4),
        }
```

---

## Solution 6: SafeToolOutputPipeline — Full Sanitization Stack

```python
import logging
import secrets
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class SafeToolOutputPipeline:
    """
    End-to-end safe rendering pipeline: sanitizes tool output,
    records dangerous inputs, and provides CSP headers for the
    HTTP response that renders the content.

    Usage:
        pipeline = SafeToolOutputPipeline()

        # In your API handler:
        safe_html = pipeline.render(
            tool_output=web_search_result,
            output_type="markdown",
            context="html",
            source="web_search",
        )
        csp_headers = pipeline.csp_headers(profile="chat_ui")
        return Response(safe_html, headers=csp_headers)
    """

    def __init__(self):
        self._base_sanitizer = HTMLAllowlistSanitizer()
        self._audit = SanitizationAuditLogger(self._base_sanitizer)
        self._renderer = ToolOutputRenderer(
            sanitizer=self._base_sanitizer,
            md_renderer=MarkdownSafeRenderer(self._base_sanitizer),
        )
        self._csp = ContentSecurityPolicyHelper()

    def render(self, tool_output: Any,
                output_type: str = "markdown",
                context: str = "html",
                source: str = "") -> str:
        # First: convert to string
        import json
        if not isinstance(tool_output, str):
            raw = json.dumps(tool_output, indent=2) if isinstance(
                tool_output, (dict, list)) else str(tool_output)
        else:
            raw = tool_output

        # Second: audit-sanitize before rendering
        sanitized = self._audit.sanitize(raw, source=source)

        # Third: render in context
        return self._renderer.render(sanitized, output_type, context)

    def csp_headers(self, profile: str = "chat_ui") -> Dict[str, str]:
        nonce = secrets.token_urlsafe(16)
        return self._csp.headers_for(profile, nonce=nonce)

    def health(self) -> Dict[str, Any]:
        return {
            "sanitization": self._audit.stats(),
            "dangerous_sources": self._audit.top_dangerous_sources(),
        }
```

---

## Comparison

| Approach | Tag Allowlist | Attribute Sanitization | URL Validation | CSP Headers | Audit Logging | Integrated |
|---|---|---|---|---|---|---|
| **HTMLAllowlistSanitizer** | Yes | Yes | Yes | No | No | No |
| **MarkdownSafeRenderer** | Via sanitizer | Via sanitizer | Via sanitizer | No | No | No |
| **ToolOutputRenderer** | Yes | Yes | Yes | No | No | No |
| **ContentSecurityPolicyHelper** | No | No | No | Yes | No | No |
| **SanitizationAuditLogger** | Via sanitizer | Via sanitizer | Via sanitizer | No | Yes | No |
| **SafeToolOutputPipeline** | Yes | Yes | Yes | Yes | Yes | Yes |

**Key insight**: use a production-grade library (`bleach`, `html-sanitizer`, or `DOMPurify` on the frontend) rather than the regex-based approach shown here — HTML parsing with regex is incomplete and bypassable. The allowlist approach is non-negotiable: a denylist that tries to enumerate all dangerous patterns will always miss edge cases (`<scr\nipt>`, `&#x6A;avascript:`, `<img src=x onerror=...>`). Add CSP headers as a second layer — even if a sanitization bypass is discovered, a `script-src 'self'` CSP prevents injected scripts from executing unless they are served from your own origin.
