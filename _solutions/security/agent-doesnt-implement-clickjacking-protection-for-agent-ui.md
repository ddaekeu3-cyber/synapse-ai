---
title: "Agent Doesn't Implement Clickjacking Protection for Agent UI"
description: "Agent web interfaces served without clickjacking protections can be embedded in attacker-controlled iframes, overlaid with invisible UI elements, and used to trick authenticated users into executing unintended agent actions. Implement X-Frame-Options, CSP frame-ancestors, and JavaScript frame-busting to prevent UI redressing attacks against agent interfaces."
date: 2026-04-16
difficulty: intermediate
category: security
slug: agent-doesnt-implement-clickjacking-protection-for-agent-ui
tags: [clickjacking, ui-redressing, x-frame-options, csp, frame-ancestors, security]
symptoms:
  - "Agent UI can be embedded in any third-party iframe without restriction"
  - "No X-Frame-Options or Content-Security-Policy frame-ancestors header on agent responses"
  - "Attacker can overlay invisible iframe over their page to capture agent clicks"
  - "Authenticated agent actions (send message, approve tool call) can be triggered via clickjacking"
  - "Agent confirmation dialogs can be overlaid and dismissed by invisible iframe interaction"
---

## Why This Happens

Clickjacking embeds a target page in a transparent iframe positioned over attacker-controlled UI. When the victim interacts with what appears to be the attacker's page, they're actually clicking elements in the hidden target iframe. For agent UIs with confirmation dialogs, approval buttons, and session tokens in cookies, this can lead to unauthorized action execution. The fix is to prevent framing entirely (for non-embeddable UIs) or restrict it to specific trusted origins.

## Solution 1: HTTP Header Middleware for Frame Protection

```python
from dataclasses import dataclass
from typing import List, Optional

@dataclass
class FrameProtectionConfig:
    mode: str = "deny"          # "deny" | "sameorigin" | "allowlist"
    allowed_origins: List[str] = None   # used when mode="allowlist"

    def __post_init__(self):
        if self.allowed_origins is None:
            self.allowed_origins = []

class FrameProtectionMiddleware:
    """
    Adds X-Frame-Options and Content-Security-Policy frame-ancestors
    headers to every response. CSP frame-ancestors supersedes
    X-Frame-Options in modern browsers; both are set for compatibility.
    """

    def __init__(self, config: FrameProtectionConfig):
        self._config = config

    def headers(self) -> dict:
        headers = {}

        if self._config.mode == "deny":
            headers["X-Frame-Options"] = "DENY"
            headers["Content-Security-Policy"] = "frame-ancestors 'none'"

        elif self._config.mode == "sameorigin":
            headers["X-Frame-Options"] = "SAMEORIGIN"
            headers["Content-Security-Policy"] = "frame-ancestors 'self'"

        elif self._config.mode == "allowlist":
            origins = " ".join(self._config.allowed_origins)
            # X-Frame-Options does not support multiple origins — use CSP only
            headers["X-Frame-Options"] = "SAMEORIGIN"   # fallback
            headers["Content-Security-Policy"] = (
                f"frame-ancestors 'self' {origins}"
            )

        # Additional hardening headers
        headers["X-Content-Type-Options"] = "nosniff"
        headers["X-XSS-Protection"] = "1; mode=block"

        return headers

    def apply_to_response(self, response) -> None:
        """Apply frame protection headers to an HTTP response object."""
        for k, v in self.headers().items():
            response.headers[k] = v


# FastAPI / Starlette middleware example
class FrameProtectionASGIMiddleware:
    def __init__(self, app, config: FrameProtectionConfig):
        self._app = app
        self._middleware = FrameProtectionMiddleware(config)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        headers_to_add = [
            (k.lower().encode(), v.encode())
            for k, v in self._middleware.headers().items()
        ]

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                message = dict(message)
                message["headers"] = list(message.get("headers", [])) + headers_to_add
            await send(message)

        await self._app(scope, receive, send_with_headers)
```

## Solution 2: JavaScript Frame-Busting Script

```python
FRAME_BUSTING_SCRIPT = """
(function() {
  'use strict';

  // Defense-in-depth: JavaScript frame detection
  // Note: CSP frame-ancestors is the primary defense;
  // this is a fallback for browsers that don't support it.

  function isFramed() {
    try {
      return window.self !== window.top;
    } catch (e) {
      // Cross-origin access throws — we're in a frame
      return true;
    }
  }

  function getTopOrigin() {
    try {
      return window.top.location.origin;
    } catch (e) {
      return null;  // cross-origin frame
    }
  }

  if (isFramed()) {
    var topOrigin = getTopOrigin();
    var allowedOrigins = __ALLOWED_ORIGINS__;

    if (!topOrigin || allowedOrigins.indexOf(topOrigin) === -1) {
      // Break out of frame or replace with warning
      try {
        window.top.location.replace(window.self.location.href);
      } catch (e) {
        // Can't navigate top frame (cross-origin) — show warning overlay
        document.body.innerHTML = '<div style="' +
          'position:fixed;top:0;left:0;width:100%;height:100%;' +
          'background:#fff;z-index:999999;display:flex;align-items:center;' +
          'justify-content:center;font-family:sans-serif;font-size:18px;' +
          'color:#c00;text-align:center;padding:20px;box-sizing:border-box">' +
          '<div>' +
          '<strong>Security Warning</strong><br><br>' +
          'This application cannot be embedded in external pages.' +
          '</div></div>';
      }
    }
  }
})();
"""

class FrameBustingScriptGenerator:
    """
    Generates frame-busting JavaScript with embedded allowed origins.
    Inject this script in the <head> of agent UI pages.
    """

    def generate(self, allowed_origins: list = None) -> str:
        origins_json = str(allowed_origins or [])
        return FRAME_BUSTING_SCRIPT.replace(
            "__ALLOWED_ORIGINS__", origins_json
        )

    def html_tag(self, allowed_origins: list = None) -> str:
        script = self.generate(allowed_origins)
        return f"<script>{script}</script>"
```

## Solution 3: Origin Validation for Agent API Endpoints

```python
from typing import List, Optional, Set
from urllib.parse import urlparse

class OriginValidator:
    """
    Validates the Origin and Referer headers on state-mutating requests.
    Rejects requests whose origin doesn't match the trusted origins list.
    Provides an extra layer of defense for AJAX requests from the agent UI.
    """

    def __init__(self, trusted_origins: List[str], strict: bool = True):
        self._trusted: Set[str] = set()
        for origin in trusted_origins:
            parsed = urlparse(origin)
            normalized = f"{parsed.scheme}://{parsed.netloc}"
            self._trusted.add(normalized)
        self._strict = strict

    def _normalize_origin(self, origin: str) -> str:
        parsed = urlparse(origin)
        return f"{parsed.scheme}://{parsed.netloc}"

    def is_trusted(self, origin: Optional[str]) -> bool:
        if not origin:
            # No Origin header: allow in non-strict mode (e.g., server-to-server)
            return not self._strict
        return self._normalize_origin(origin) in self._trusted

    def validate_request(self, origin: Optional[str], referer: Optional[str]) -> dict:
        origin_ok = self.is_trusted(origin)
        referer_ok = True

        if referer:
            # Extract origin from referer URL
            referer_origin = self._normalize_origin(referer)
            referer_ok = referer_origin in self._trusted

        if self._strict:
            allowed = origin_ok and referer_ok
        else:
            allowed = origin_ok or referer_ok or (not origin and not referer)

        return {
            "allowed": allowed,
            "origin": origin,
            "origin_trusted": origin_ok,
            "referer": referer,
            "referer_trusted": referer_ok,
        }
```

## Solution 4: CSRF + Clickjacking Combined Guard

```python
import hashlib
import hmac
import secrets
import time
from typing import Optional

class CombinedUISecurityGuard:
    """
    Combines clickjacking protection with CSRF token validation.
    Clickjacking allows an attacker to trigger clicks; CSRF protection
    ensures those clicks can't execute state-mutating operations without
    a valid token that the attacker cannot read across frames.
    """

    def __init__(self, secret_key: bytes, token_ttl_seconds: int = 3600):
        self._secret = secret_key
        self._ttl = token_ttl_seconds

    def generate_csrf_token(self, session_id: str) -> str:
        """Generate a time-bound CSRF token tied to the session."""
        timestamp = int(time.time())
        message = f"{session_id}:{timestamp}".encode()
        mac = hmac.new(self._secret, message, hashlib.sha256).hexdigest()
        return f"{timestamp}:{mac}"

    def validate_csrf_token(self, token: str, session_id: str) -> dict:
        """Validates a CSRF token. Returns {valid, reason}."""
        try:
            parts = token.split(":", 1)
            if len(parts) != 2:
                return {"valid": False, "reason": "malformed_token"}
            timestamp_str, mac = parts
            timestamp = int(timestamp_str)
        except (ValueError, AttributeError):
            return {"valid": False, "reason": "parse_error"}

        if time.time() - timestamp > self._ttl:
            return {"valid": False, "reason": "token_expired"}

        message = f"{session_id}:{timestamp}".encode()
        expected_mac = hmac.new(self._secret, message, hashlib.sha256).hexdigest()

        if not hmac.compare_digest(mac, expected_mac):
            return {"valid": False, "reason": "invalid_signature"}

        return {"valid": True, "reason": "ok"}

    def security_headers(self, frame_mode: str = "deny") -> dict:
        """Returns the combined set of security headers."""
        headers = {
            "X-Frame-Options": "DENY" if frame_mode == "deny" else "SAMEORIGIN",
            "Content-Security-Policy": (
                "frame-ancestors 'none'" if frame_mode == "deny"
                else "frame-ancestors 'self'"
            ),
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "strict-origin-when-cross-origin",
            "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
        }
        return headers
```

## Solution 5: Framing Attempt Detector

```python
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Deque, Dict, List

@dataclass
class FramingAttempt:
    source_ip: str
    origin: Optional[str]
    referer: Optional[str]
    path: str
    timestamp: float

class FramingAttemptDetector:
    """
    Detects and logs attempts to frame the agent UI in unauthorized origins.
    Repeated attempts from the same IP or origin may indicate active attack.
    """

    def __init__(self, window_seconds: float = 3600.0, alert_threshold: int = 5):
        self._window = window_seconds
        self._threshold = alert_threshold
        self._attempts: Deque[FramingAttempt] = deque(maxlen=1000)
        self._by_ip: Dict[str, Deque[float]] = defaultdict(lambda: deque(maxlen=100))
        self._by_origin: Dict[str, Deque[float]] = defaultdict(lambda: deque(maxlen=100))

    def record_attempt(
        self,
        source_ip: str,
        origin: Optional[str],
        referer: Optional[str],
        path: str,
    ) -> bool:
        """Records a framing attempt. Returns True if alert threshold is exceeded."""
        now = time.time()
        attempt = FramingAttempt(
            source_ip=source_ip, origin=origin, referer=referer,
            path=path, timestamp=now,
        )
        self._attempts.append(attempt)
        self._by_ip[source_ip].append(now)
        if origin:
            self._by_origin[origin].append(now)

        # Check alert threshold
        cutoff = now - self._window
        ip_count = sum(1 for t in self._by_ip[source_ip] if t >= cutoff)
        if ip_count >= self._threshold:
            print(
                f"[frame_detector] ALERT: {ip_count} framing attempts from IP {source_ip} "
                f"in last {self._window}s"
            )
            return True
        return False

    def summary(self, window_seconds: Optional[float] = None) -> dict:
        window = window_seconds or self._window
        cutoff = time.time() - window
        recent = [a for a in self._attempts if a.timestamp >= cutoff]
        return {
            "attempts_in_window": len(recent),
            "unique_ips": len({a.source_ip for a in recent}),
            "unique_origins": len({a.origin for a in recent if a.origin}),
            "top_paths": list(
                {a.path for a in sorted(recent, key=lambda x: x.timestamp, reverse=True)[:10]}
            ),
        }
```

## Solution 6: Security Header Audit

```python
from typing import Dict, List

class SecurityHeaderAuditor:
    """
    Validates that all required frame protection headers are present
    and correctly configured. Use in CI or health checks.
    """

    REQUIRED_HEADERS = {
        "x-frame-options": ["DENY", "SAMEORIGIN"],
        "content-security-policy": None,   # checked separately
        "x-content-type-options": ["nosniff"],
    }

    def audit(self, response_headers: Dict[str, str]) -> dict:
        normalized = {k.lower(): v for k, v in response_headers.items()}
        findings = []
        passed = []

        for header, allowed_values in self.REQUIRED_HEADERS.items():
            if header not in normalized:
                findings.append({"header": header, "issue": "missing"})
                continue
            value = normalized[header]
            if allowed_values and value not in allowed_values:
                findings.append({
                    "header": header,
                    "issue": f"unexpected_value:{value}",
                    "expected_one_of": allowed_values,
                })
            else:
                passed.append(header)

        # Check CSP frame-ancestors specifically
        csp = normalized.get("content-security-policy", "")
        if "frame-ancestors" not in csp:
            findings.append({
                "header": "content-security-policy",
                "issue": "missing_frame-ancestors_directive",
            })
        else:
            passed.append("csp:frame-ancestors")

        return {
            "passed": passed,
            "findings": findings,
            "secure": len(findings) == 0,
        }
```

## Comparison

| Approach | Server-Side | Client-Side | CSRF Defense | Attack Detection |
|---|---|---|---|---|
| FrameProtectionMiddleware | Yes (headers) | No | No | No |
| FrameBustingScriptGenerator | No | Yes (JS) | No | No |
| OriginValidator | Yes (validation) | No | Partial | No |
| CombinedUISecurityGuard | Yes (headers + CSRF) | No | Yes | No |
| FramingAttemptDetector | Yes (logging) | No | No | Yes |
| SecurityHeaderAuditor | Yes (audit) | No | No | No |

**Best for production**: Set `Content-Security-Policy: frame-ancestors 'none'` and `X-Frame-Options: DENY` via `FrameProtectionMiddleware` on every agent UI response — this is the primary defense. Add `FrameBustingScriptGenerator` as defense-in-depth for older browsers. Protect all state-mutating endpoints with `CombinedUISecurityGuard` CSRF tokens. Run `SecurityHeaderAuditor` in CI against staging responses to catch misconfiguration before deploy. Log all suspicious origin mismatches via `FramingAttemptDetector` to identify active attack campaigns.
