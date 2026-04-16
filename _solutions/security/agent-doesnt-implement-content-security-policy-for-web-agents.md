---
title: "Agent Doesn't Implement Content Security Policy for Web Agents"
description: "How to configure and enforce Content Security Policy (CSP), Subresource Integrity (SRI), iframe sandboxing, and related browser security headers for AI agents that render output in web contexts — preventing XSS, clickjacking, and data exfiltration."
date: 2025-01-16
difficulty: intermediate
category: security
slug: agent-doesnt-implement-content-security-policy-for-web-agents
tags:
  - security
  - content-security-policy
  - csp
  - xss-prevention
  - web-security
  - http-headers
  - clickjacking
symptoms:
  - "Agent renders user-provided or LLM-generated HTML without XSS protection"
  - "No CSP header prevents injected scripts from executing in agent web UI"
  - "Agent iframe embeds are vulnerable to clickjacking attacks"
  - "LLM-generated markdown rendered to HTML can include malicious script tags"
  - "No Subresource Integrity checks on CDN-loaded scripts in agent dashboard"
  - "Agent API responses lack security headers required by browser security audits"
---

## Why This Happens

AI agents that serve web UIs, render LLM output as HTML, or embed third-party tools in iframes are exposed to a class of vulnerabilities that purely server-side agents are not. LLM outputs can contain markdown that, when rendered without sanitization, introduces XSS vectors. Third-party scripts loaded in the agent dashboard can be compromised. Without a Content Security Policy, the browser has no way to distinguish legitimate agent code from injected malicious scripts.

CSP is a browser-enforced security boundary: the server declares exactly which sources of content are allowed, and the browser refuses to execute anything else. Paired with output sanitization, SRI, and anti-clickjacking headers, CSP dramatically reduces the attack surface of web-facing AI agent systems.

---

## Solution 1: Core CSP Header Builder

Build a strict, nonce-based CSP header for agent web UIs that blocks inline scripts and allows only explicitly whitelisted sources.

```python
import secrets
import hashlib
import base64
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class CSPDirective:
    """Represents a single CSP directive with its allowed sources."""
    directive: str
    sources: list[str] = field(default_factory=list)
    nonce: Optional[str] = None

    def __str__(self) -> str:
        parts = [self.directive]
        if self.nonce:
            parts.append(f"'nonce-{self.nonce}'")
        parts.extend(self.sources)
        return " ".join(parts)


class ContentSecurityPolicy:
    """
    Builds a strict Content Security Policy for AI agent web applications.
    Uses per-request nonces for inline scripts to allow legitimate inline code
    while blocking injected scripts.
    """

    def __init__(self):
        self._nonce = secrets.token_urlsafe(16)
        self._directives: dict[str, CSPDirective] = {}
        self._report_uri: Optional[str] = None
        self._report_only: bool = False

    @classmethod
    def strict(cls, report_uri: Optional[str] = None) -> "ContentSecurityPolicy":
        """Create a strict CSP suitable for most agent web UIs."""
        csp = cls()
        csp._report_uri = report_uri
        return (csp
            .default_src("'none'")
            .script_src("'self'", nonce=True)
            .style_src("'self'", "'unsafe-inline'")  # Allow inline styles for convenience
            .img_src("'self'", "data:", "https:")
            .connect_src("'self'")
            .font_src("'self'")
            .object_src("'none'")
            .frame_src("'none'")
            .frame_ancestors("'none'")
            .form_action("'self'")
            .base_uri("'self'")
            .upgrade_insecure_requests()
        )

    @property
    def nonce(self) -> str:
        return self._nonce

    def _add(self, directive: str, *sources: str, nonce: bool = False) -> "ContentSecurityPolicy":
        d = CSPDirective(directive=directive, sources=list(sources))
        if nonce:
            d.nonce = self._nonce
        self._directives[directive] = d
        return self

    def default_src(self, *sources: str) -> "ContentSecurityPolicy":
        return self._add("default-src", *sources)

    def script_src(self, *sources: str, nonce: bool = False) -> "ContentSecurityPolicy":
        return self._add("script-src", *sources, nonce=nonce)

    def style_src(self, *sources: str) -> "ContentSecurityPolicy":
        return self._add("style-src", *sources)

    def img_src(self, *sources: str) -> "ContentSecurityPolicy":
        return self._add("img-src", *sources)

    def connect_src(self, *sources: str) -> "ContentSecurityPolicy":
        return self._add("connect-src", *sources)

    def font_src(self, *sources: str) -> "ContentSecurityPolicy":
        return self._add("font-src", *sources)

    def object_src(self, *sources: str) -> "ContentSecurityPolicy":
        return self._add("object-src", *sources)

    def frame_src(self, *sources: str) -> "ContentSecurityPolicy":
        return self._add("frame-src", *sources)

    def frame_ancestors(self, *sources: str) -> "ContentSecurityPolicy":
        return self._add("frame-ancestors", *sources)

    def form_action(self, *sources: str) -> "ContentSecurityPolicy":
        return self._add("form-action", *sources)

    def base_uri(self, *sources: str) -> "ContentSecurityPolicy":
        return self._add("base-uri", *sources)

    def upgrade_insecure_requests(self) -> "ContentSecurityPolicy":
        self._directives["upgrade-insecure-requests"] = CSPDirective("upgrade-insecure-requests")
        return self

    def allow_script_hash(self, script_content: str) -> "ContentSecurityPolicy":
        """Allow a specific inline script by its SHA-256 hash."""
        digest = hashlib.sha256(script_content.encode()).digest()
        hash_b64 = base64.b64encode(digest).decode()
        d = self._directives.get("script-src")
        if d:
            d.sources.append(f"'sha256-{hash_b64}'")
        return self

    def header_value(self) -> str:
        parts = []
        for directive in self._directives.values():
            s = str(directive)
            if s:
                parts.append(s)
        if self._report_uri:
            parts.append(f"report-uri {self._report_uri}")
        return "; ".join(parts)

    def header_name(self) -> str:
        if self._report_only:
            return "Content-Security-Policy-Report-Only"
        return "Content-Security-Policy"

    def as_report_only(self) -> "ContentSecurityPolicy":
        self._report_only = True
        return self


# --- Usage ---

def demo_csp():
    csp = ContentSecurityPolicy.strict(report_uri="/csp-report")
    print(f"{csp.header_name()}: {csp.header_value()}")
    print(f"Nonce for this request: {csp.nonce}")
    # Use csp.nonce in <script nonce="..."> tags in templates
```

---

## Solution 2: Full Security Headers Middleware

A complete HTTP security headers middleware for FastAPI/Starlette agent servers.

```python
from typing import Callable
import re

class SecurityHeadersMiddleware:
    """
    ASGI middleware that adds all recommended security headers to every response.
    Generates a fresh CSP nonce per request and injects it into response context.
    """

    def __init__(
        self,
        app,
        csp_report_uri: Optional[str] = None,
        allowed_frame_ancestors: list[str] | None = None,
        hsts_max_age: int = 63_072_000,  # 2 years
    ):
        self.app = app
        self.csp_report_uri = csp_report_uri
        self.allowed_frame_ancestors = allowed_frame_ancestors or []
        self.hsts_max_age = hsts_max_age

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        nonce = secrets.token_urlsafe(16)
        scope["csp_nonce"] = nonce

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                for name, value in self._security_headers(nonce).items():
                    headers.append((name.encode(), value.encode()))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)

    def _security_headers(self, nonce: str) -> dict[str, str]:
        frame_ancestors = " ".join(self.allowed_frame_ancestors) if self.allowed_frame_ancestors else "'none'"
        csp = ContentSecurityPolicy.strict(self.csp_report_uri)
        csp._nonce = nonce

        headers = {
            csp.header_name(): csp.header_value(),
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY" if not self.allowed_frame_ancestors else "SAMEORIGIN",
            "X-XSS-Protection": "0",  # Disabled in favour of CSP
            "Referrer-Policy": "strict-origin-when-cross-origin",
            "Permissions-Policy": (
                "camera=(), microphone=(), geolocation=(), "
                "payment=(), usb=(), bluetooth=()"
            ),
            "Cross-Origin-Opener-Policy": "same-origin",
            "Cross-Origin-Embedder-Policy": "require-corp",
            "Cross-Origin-Resource-Policy": "same-origin",
        }
        if self.hsts_max_age:
            headers["Strict-Transport-Security"] = (
                f"max-age={self.hsts_max_age}; includeSubDomains; preload"
            )
        return headers
```

---

## Solution 3: LLM Output HTML Sanitizer

Sanitize HTML generated by the LLM before rendering in the browser — strip dangerous tags and attributes while preserving safe formatting.

```python
import re
from html.parser import HTMLParser

class LLMOutputSanitizer:
    """
    Sanitizes LLM-generated HTML to remove XSS vectors before browser rendering.
    Allowlist-based: only explicitly permitted tags and attributes pass through.
    """

    ALLOWED_TAGS = {
        "p", "br", "strong", "em", "b", "i", "u", "s", "code", "pre",
        "h1", "h2", "h3", "h4", "h5", "h6",
        "ul", "ol", "li", "blockquote",
        "a", "img",
        "table", "thead", "tbody", "tr", "th", "td",
        "div", "span",
    }

    ALLOWED_ATTRS = {
        "a":   {"href", "title", "rel"},
        "img": {"src", "alt", "width", "height"},
        "*":   {"class", "id"},
    }

    SAFE_URL_SCHEMES = {"https", "http", "mailto"}

    DANGEROUS_PATTERNS = [
        re.compile(r"javascript\s*:", re.IGNORECASE),
        re.compile(r"data\s*:", re.IGNORECASE),
        re.compile(r"vbscript\s*:", re.IGNORECASE),
        re.compile(r"on\w+\s*=", re.IGNORECASE),  # onclick, onload, etc.
    ]

    def sanitize(self, html: str) -> str:
        """Sanitize HTML string, returning safe subset."""
        sanitizer = _HTMLSanitizerParser(
            self.ALLOWED_TAGS,
            self.ALLOWED_ATTRS,
            self.SAFE_URL_SCHEMES,
            self.DANGEROUS_PATTERNS,
        )
        sanitizer.feed(html)
        return sanitizer.output

    def sanitize_url(self, url: str) -> str:
        """Ensure URL uses a safe scheme."""
        url = url.strip()
        for pattern in self.DANGEROUS_PATTERNS:
            if pattern.search(url):
                return "#"
        scheme = url.split(":")[0].lower() if ":" in url else ""
        if scheme and scheme not in self.SAFE_URL_SCHEMES:
            return "#"
        return url


class _HTMLSanitizerParser(HTMLParser):
    def __init__(self, allowed_tags, allowed_attrs, safe_schemes, dangerous):
        super().__init__()
        self.allowed_tags = allowed_tags
        self.allowed_attrs = allowed_attrs
        self.safe_schemes = safe_schemes
        self.dangerous = dangerous
        self.output = ""
        self._skip_stack: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag not in self.allowed_tags:
            self._skip_stack.append(tag)
            return
        safe_attrs = self._filter_attrs(tag, attrs)
        attr_str = "".join(f' {k}="{v}"' for k, v in safe_attrs.items())
        self.output += f"<{tag}{attr_str}>"

    def handle_endtag(self, tag: str) -> None:
        if self._skip_stack and self._skip_stack[-1] == tag:
            self._skip_stack.pop()
            return
        if tag in self.allowed_tags:
            self.output += f"</{tag}>"

    def handle_data(self, data: str) -> None:
        if not self._skip_stack:
            import html
            self.output += html.escape(data, quote=False)

    def _filter_attrs(self, tag: str, attrs: list) -> dict:
        tag_allowed = self.allowed_attrs.get(tag, set()) | self.allowed_attrs.get("*", set())
        result = {}
        for name, value in attrs:
            if name not in tag_allowed:
                continue
            if value is None:
                continue
            # Sanitize URLs
            if name in ("href", "src", "action"):
                value = value.strip()
                for pattern in self.dangerous:
                    if pattern.search(value):
                        value = "#"
                        break
            result[name] = value
        return result
```

---

## Solution 4: Nonce-Based Template Integration

Inject CSP nonces into HTML templates so legitimate inline scripts use the nonce while injected scripts are blocked.

```python
from string import Template

class NonceTemplateRenderer:
    """
    Template renderer that automatically injects the per-request CSP nonce
    into all <script> and <style> tags in the template.
    """

    SCRIPT_TAG_RE = re.compile(r"<script(?P<attrs>[^>]*)>", re.IGNORECASE)
    STYLE_TAG_RE  = re.compile(r"<style(?P<attrs>[^>]*)>", re.IGNORECASE)

    def __init__(self, template: str):
        self.template = template

    def render(self, nonce: str, context: dict | None = None) -> str:
        """Render template with nonce injected into script/style tags."""
        html = self.template

        def inject_nonce(match: re.Match) -> str:
            tag = match.group(0)
            attrs = match.group("attrs")
            if "nonce" not in attrs:
                tag_name = "script" if "<script" in tag.lower() else "style"
                return f"<{tag_name}{attrs} nonce=\"{nonce}\">"
            return tag

        html = self.SCRIPT_TAG_RE.sub(inject_nonce, html)
        html = self.STYLE_TAG_RE.sub(inject_nonce, html)

        if context:
            import html as html_module
            safe_context = {k: html_module.escape(str(v)) for k, v in context.items()}
            html = Template(html).safe_substitute(safe_context)

        return html


# --- FastAPI integration example ---

class AgentWebServer:
    """
    Example FastAPI server with CSP and output sanitization.
    """

    def __init__(self):
        self.sanitizer = LLMOutputSanitizer()
        self._template = NonceTemplateRenderer("""
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Agent UI</title>
  <script>
    // This script gets nonce automatically
    const agentConfig = $agent_config;
  </script>
</head>
<body>
  <div id="response">$agent_response</div>
</body>
</html>
""")

    def render_response(self, nonce: str, llm_output: str, config: dict) -> str:
        import json
        import html
        safe_output = self.sanitizer.sanitize(llm_output)
        return self._template.render(nonce, {
            "agent_response": safe_output,
            "agent_config": json.dumps(config),
        })
```

---

## Solution 5: CSP Violation Report Handler

Collect and analyze CSP violation reports to detect active injection attempts and policy gaps.

```python
from dataclasses import dataclass
import time
import json

@dataclass
class CSPViolationReport:
    timestamp: float
    document_uri: str
    violated_directive: str
    blocked_uri: str
    source_file: str
    line_number: int
    column_number: int
    original_policy: str
    severity: str  # "info", "warning", "critical"

class CSPViolationHandler:
    """
    Parses, categorizes, and stores CSP violation reports.
    Detects patterns indicating active XSS attempts vs. policy gaps.
    """

    CRITICAL_DIRECTIVES = {"script-src", "object-src", "frame-ancestors"}

    def __init__(self):
        self._violations: list[CSPViolationReport] = []
        self._alert_callbacks: list[Callable] = []

    def parse_report(self, raw_body: str) -> CSPViolationReport | None:
        """Parse a CSP report-uri POST body."""
        try:
            data = json.loads(raw_body).get("csp-report", {})
            directive = data.get("violated-directive", "")
            severity = "critical" if any(d in directive for d in self.CRITICAL_DIRECTIVES) else "info"
            return CSPViolationReport(
                timestamp=time.time(),
                document_uri=data.get("document-uri", ""),
                violated_directive=directive,
                blocked_uri=data.get("blocked-uri", ""),
                source_file=data.get("source-file", ""),
                line_number=int(data.get("line-number", 0)),
                column_number=int(data.get("column-number", 0)),
                original_policy=data.get("original-policy", ""),
                severity=severity,
            )
        except (json.JSONDecodeError, KeyError, ValueError):
            return None

    async def handle(self, raw_body: str) -> None:
        report = self.parse_report(raw_body)
        if report is None:
            return
        self._violations.append(report)

        if report.severity == "critical":
            for cb in self._alert_callbacks:
                await cb(report)

    def on_critical(self, callback: Callable) -> None:
        self._alert_callbacks.append(callback)

    def get_recent(self, minutes: float = 60.0) -> list[CSPViolationReport]:
        cutoff = time.time() - minutes * 60
        return [v for v in self._violations if v.timestamp >= cutoff]

    def summary(self) -> dict:
        violations = self._violations
        by_directive: dict[str, int] = {}
        by_blocked_uri: dict[str, int] = {}
        for v in violations:
            by_directive[v.violated_directive] = by_directive.get(v.violated_directive, 0) + 1
            by_blocked_uri[v.blocked_uri] = by_blocked_uri.get(v.blocked_uri, 0) + 1
        return {
            "total": len(violations),
            "critical": sum(1 for v in violations if v.severity == "critical"),
            "top_directives": sorted(by_directive.items(), key=lambda x: -x[1])[:5],
            "top_blocked_uris": sorted(by_blocked_uri.items(), key=lambda x: -x[1])[:5],
        }
```

---

## Solution 6: Iframe Sandbox Policy Builder

Configure strict sandbox policies for iframes that embed third-party tools or render agent output.

```python
class IframeSandboxPolicy:
    """
    Builds iframe sandbox attribute values for safely embedding content.
    Starts from a deny-all baseline and selectively grants permissions.
    """

    ALL_PERMISSIONS = {
        "allow-downloads",
        "allow-forms",
        "allow-modals",
        "allow-orientation-lock",
        "allow-pointer-lock",
        "allow-popups",
        "allow-popups-to-escape-sandbox",
        "allow-presentation",
        "allow-same-origin",
        "allow-scripts",
        "allow-storage-access-by-user-activation",
        "allow-top-navigation",
        "allow-top-navigation-by-user-activation",
    }

    def __init__(self):
        self._grants: set[str] = set()

    @classmethod
    def read_only(cls) -> "IframeSandboxPolicy":
        """Display-only iframe: no scripts, no forms, no navigation."""
        return cls()

    @classmethod
    def interactive(cls) -> "IframeSandboxPolicy":
        """Interactive iframe: scripts allowed but isolated origin."""
        return cls().allow_scripts()

    @classmethod
    def trusted_app(cls) -> "IframeSandboxPolicy":
        """For trusted first-party embedded apps."""
        return (cls()
            .allow_scripts()
            .allow_forms()
            .allow_same_origin()
            .allow_popups()
        )

    def allow_scripts(self) -> "IframeSandboxPolicy":
        self._grants.add("allow-scripts")
        return self

    def allow_forms(self) -> "IframeSandboxPolicy":
        self._grants.add("allow-forms")
        return self

    def allow_same_origin(self) -> "IframeSandboxPolicy":
        self._grants.add("allow-same-origin")
        return self

    def allow_popups(self) -> "IframeSandboxPolicy":
        self._grants.add("allow-popups")
        return self

    def allow_navigation(self) -> "IframeSandboxPolicy":
        self._grants.add("allow-top-navigation-by-user-activation")
        return self

    def sandbox_attribute(self) -> str:
        """Return the value for the iframe sandbox= attribute."""
        if not self._grants:
            return "sandbox"  # Deny all
        return "sandbox=\"" + " ".join(sorted(self._grants)) + "\""

    def csp_frame_src(self, origin: str) -> str:
        """Generate frame-src CSP directive for this iframe's origin."""
        return f"frame-src {origin}"

    def html_tag(self, src: str, width: int = 600, height: int = 400) -> str:
        grants_str = " ".join(sorted(self._grants)) if self._grants else ""
        sandbox_val = f'sandbox="{grants_str}"' if grants_str else "sandbox"
        return (
            f'<iframe src="{src}" width="{width}" height="{height}" '
            f'{sandbox_val} '
            f'referrerpolicy="no-referrer" '
            f'loading="lazy">'
            f'</iframe>'
        )
```

---

## Comparison

| Solution | Protection Type | Browser Enforcement | Deployment Effort | Best For |
|---|---|---|---|---|
| CSP Header Builder | Script/resource injection | Yes | Low | All web-facing agents |
| Security Headers Middleware | Multiple vectors | Yes | Low (middleware) | FastAPI/Starlette servers |
| LLM Output Sanitizer | XSS via LLM output | No (server-side) | Low | Agents rendering LLM HTML |
| Nonce Template Renderer | Inline script injection | Yes | Medium | Template-based UIs |
| CSP Violation Handler | Detection + alerting | No (reporting) | Low | Monitoring active attacks |
| Iframe Sandbox | Third-party content | Yes | Low | Embedded tool UIs |

**Always deploy the security headers middleware** — it's a one-line addition that provides defense-in-depth with no functionality cost. **Add the LLM output sanitizer** anywhere the agent renders model-generated content as HTML. **Use nonce-based CSP** (not `unsafe-inline`) for script tags to block injected scripts while allowing legitimate inline code. **Configure the violation handler** to get visibility into attacks and policy gaps before switching from report-only to enforced mode. **Use iframe sandbox policies** for any third-party tool embedding in the agent dashboard.
