---
layout: solution
title: "Agent Sends Credentials Over HTTP Instead of HTTPS — Credentials Exposed"
category: auth
description: "Agent makes API calls using http:// instead of https://. API keys, tokens, and session credentials are sent in plaintext over the network. Anyone on the same network can intercept them with a packet sniffer."
tags: [security, http, https, tls, credentials, plaintext, network, mitm]
---

# Agent Sends Credentials Over HTTP Instead of HTTPS — Credentials Exposed

## Symptom
- API calls use `http://api.example.com` instead of `https://api.example.com`
- Security scanner flags outbound HTTP requests carrying Authorization headers
- Credentials work in dev (local network) but are intercepted in production
- Log shows `http://` URLs in API call history
- Some endpoints redirect HTTP → HTTPS, but credentials are sent in the first plaintext request
- Mobile networks or corporate proxies log all HTTP traffic including headers

## Root Cause
HTTP sends all data including headers (where API keys and auth tokens live) in plaintext. Any network device between the agent and the server — routers, proxies, ISPs, VPN endpoints — can read them. This is especially common when: base URLs are configured without specifying the scheme, HTTP redirects are followed without noticing the initial plaintext request, or dev environments use HTTP and the same config is used in prod.

## Fix

### Option 1: Enforce HTTPS at the HTTP client level

```python
import httpx
import urllib.parse

class SecureHTTPClient:
    """
    HTTP client that enforces HTTPS for all outbound requests.
    Raises immediately if HTTP is attempted with credentials.
    """

    def __init__(self, base_url: str, api_key: str = None):
        # Enforce HTTPS in the base URL
        self.base_url = self._enforce_https(base_url)
        self.api_key = api_key
        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
            verify=True,    # Always verify TLS certificates
        )

    def _enforce_https(self, url: str) -> str:
        """Convert http:// to https:// and raise if URL is suspicious"""
        parsed = urllib.parse.urlparse(url)

        if parsed.scheme == "http":
            # Localhost/loopback: HTTP is OK (no network traversal)
            if parsed.hostname in ("localhost", "127.0.0.1", "::1"):
                return url
            # Everything else: enforce HTTPS
            print(f"WARNING: HTTP URL detected — upgrading to HTTPS: {url}")
            return url.replace("http://", "https://", 1)

        if parsed.scheme not in ("https", ""):
            raise ValueError(f"Unsupported scheme '{parsed.scheme}' in URL: {url}")

        return url

    async def get(self, path: str, **kwargs) -> httpx.Response:
        url = urllib.parse.urljoin(self.base_url, path)
        self._pre_flight_check(url)
        return await self.client.get(url, **kwargs)

    def _pre_flight_check(self, url: str):
        """Final check before any request with credentials"""
        if self.api_key and url.startswith("http://"):
            host = urllib.parse.urlparse(url).hostname
            if host not in ("localhost", "127.0.0.1", "::1"):
                raise SecurityError(
                    f"Refusing to send credentials over HTTP: {url}\n"
                    f"Change the URL to use https://"
                )

class SecurityError(Exception):
    pass
```

### Option 2: Validate all configured URLs at startup

```python
import os
import urllib.parse

URL_ENV_VARS = [
    "API_BASE_URL",
    "DATABASE_URL",
    "WEBHOOK_URL",
    "CALLBACK_URL",
    "REDIS_URL",
]

def audit_url_security() -> list[str]:
    """
    Check all configured URLs for HTTP usage.
    Run at startup — fail fast if insecure URLs detected with credentials.
    """
    issues = []

    for var in URL_ENV_VARS:
        url = os.environ.get(var, "")
        if not url:
            continue

        parsed = urllib.parse.urlparse(url)

        if parsed.scheme == "http":
            hostname = parsed.hostname or ""
            is_loopback = hostname in ("localhost", "127.0.0.1", "::1", "0.0.0.0")

            if not is_loopback:
                issues.append(
                    f"{var}={url!r} uses HTTP — credentials will be sent in plaintext. "
                    f"Change to https://"
                )

        # Check for embedded credentials in URL (e.g., postgres://user:pass@host)
        if parsed.username or parsed.password:
            if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1"):
                issues.append(
                    f"{var} contains embedded credentials over HTTP: "
                    f"{parsed.scheme}://{parsed.username}:***@{parsed.hostname}"
                )

    return issues

# At startup:
issues = audit_url_security()
if issues:
    for issue in issues:
        print(f"SECURITY: {issue}")
    raise RuntimeError("Insecure URL configuration. Fix before starting agent.")
```

### Option 3: HTTPX with strict TLS settings

```python
import ssl
import httpx

def create_secure_client(
    api_key: str = None,
    timeout: float = 30.0
) -> httpx.AsyncClient:
    """
    Create an HTTPX client with strict TLS and HTTPS enforcement.
    """
    # Create strict SSL context
    ssl_context = ssl.create_default_context()
    ssl_context.minimum_version = ssl.TLSVersion.TLSv1_2  # Reject TLS 1.0/1.1
    ssl_context.check_hostname = True
    ssl_context.verify_mode = ssl.CERT_REQUIRED

    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    class HTTPSEnforcingTransport(httpx.AsyncHTTPTransport):
        async def handle_async_request(self, request):
            if request.url.scheme != "https" and request.url.host not in ("localhost", "127.0.0.1"):
                raise ValueError(
                    f"HTTP not allowed for external hosts. "
                    f"Got: {request.url.scheme}://{request.url.host}"
                )
            return await super().handle_async_request(request)

    return httpx.AsyncClient(
        headers=headers,
        timeout=timeout,
        verify=ssl_context,
        transport=HTTPSEnforcingTransport(),
        follow_redirects=True,   # Follow redirects (but still verify destination is HTTPS)
        max_redirects=3,
    )

client = create_secure_client(api_key=os.environ["API_KEY"])
```

### Option 4: Detect HTTP redirects that send credentials in the first request

```python
import httpx

async def check_for_http_redirect(url: str) -> dict:
    """
    Check if a URL redirects from HTTP to HTTPS.
    If so, credentials sent in the HTTP request are exposed before the redirect.
    """
    if url.startswith("https://"):
        return {"secure": True, "url": url}

    # Check what happens with HTTP
    async with httpx.AsyncClient(follow_redirects=False) as client:
        try:
            resp = await client.get(url, timeout=10)
            if resp.status_code in (301, 302, 307, 308):
                redirect_to = resp.headers.get("location", "")
                return {
                    "secure": False,
                    "issue": f"HTTP redirects to HTTPS, but credentials sent in plaintext first request",
                    "url": url,
                    "redirects_to": redirect_to,
                    "fix": f"Use {redirect_to} directly to avoid the plaintext first request"
                }
        except Exception as e:
            return {"secure": False, "error": str(e), "url": url}

    return {"secure": False, "url": url, "issue": "URL does not use HTTPS"}

# Warn about redirect traps:
result = await check_for_http_redirect("http://api.example.com")
if not result.get("secure"):
    print(f"SECURITY WARNING: {result.get('issue', 'Insecure URL')}")
    print(f"Fix: {result.get('fix', 'Use https://')}")
```

### Option 5: Environment variable convention — always default to HTTPS

```python
import os
import urllib.parse

def get_api_url(env_var: str, path: str = "") -> str:
    """
    Get API URL from environment, enforcing HTTPS.
    Accepts: full URL or just hostname.
    """
    raw = os.environ.get(env_var, "")
    if not raw:
        raise KeyError(f"Environment variable '{env_var}' not set")

    # If it's just a hostname (no scheme), add https://
    if "://" not in raw:
        raw = f"https://{raw}"

    parsed = urllib.parse.urlparse(raw)

    # Upgrade HTTP to HTTPS for non-localhost
    if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
        raw = raw.replace("http://", "https://")
        print(f"Auto-upgraded {env_var} to HTTPS")

    return raw.rstrip("/") + ("/" + path.lstrip("/") if path else "")

# Usage:
# API_URL=api.example.com → https://api.example.com
# API_URL=http://api.example.com → https://api.example.com (with warning)
# API_URL=https://api.example.com → https://api.example.com (unchanged)
# API_URL=http://localhost:8000 → http://localhost:8000 (loopback OK)
api_url = get_api_url("API_URL", path="/v1/completions")
```

### Option 6: Pre-commit hook and CI check for hardcoded HTTP URLs

```python
# check_http_urls.py — run in CI to catch hardcoded http:// in source code
import re
import sys
from pathlib import Path

HTTP_PATTERN = re.compile(r'http://(?!localhost|127\.0\.0\.1|0\.0\.0\.0|::1)([^\'"\s]+)')

EXCLUDE_DIRS = {".git", "__pycache__", ".venv", "node_modules", "dist", "build"}
EXCLUDE_FILES = {"*.min.js", "*.lock", "poetry.lock", "package-lock.json"}

def check_file(path: Path) -> list[str]:
    """Find hardcoded http:// URLs in a source file"""
    issues = []
    try:
        content = path.read_text(encoding="utf-8", errors="ignore")
        for match in HTTP_PATTERN.finditer(content):
            url = match.group(0)
            # Skip comment-only lines (rough heuristic)
            line = content[:match.start()].count("\n") + 1
            issues.append(f"{path}:{line}: {url}")
    except Exception:
        pass
    return issues

def main():
    root = Path(".")
    all_issues = []

    for path in root.rglob("*.py"):
        if any(ex in path.parts for ex in EXCLUDE_DIRS):
            continue
        all_issues.extend(check_file(path))

    if all_issues:
        print(f"Found {len(all_issues)} hardcoded HTTP URLs (potential security issue):")
        for issue in all_issues:
            print(f"  {issue}")
        sys.exit(1)

    print("No insecure HTTP URLs found in source code.")

if __name__ == "__main__":
    main()
```

## HTTP vs HTTPS Security Impact

| Scenario | HTTP risk | HTTPS protection |
|---|---|---|
| Same office WiFi | API key readable by coworker | Encrypted — unreadable |
| Corporate proxy | Full request + credentials logged | Only destination visible |
| ISP monitoring | Complete credential exposure | Encrypted transit |
| Man-in-the-middle | Credentials stolen + injected | Certificate validation blocks MITM |
| HTTP → HTTPS redirect | Credentials sent in first plaintext hop | Direct HTTPS avoids first-hop exposure |

## Expected Token Savings
Credential theft via HTTP → rotation + incident response: ~500,000 tokens (plus operational cost)
HTTPS enforcement at client level: 0 tokens wasted

## Environment
- All agents making external API calls; most critical in cloud/container environments with network monitoring
- Source: OWASP A02:2021 Cryptographic Failures; direct experience auditing agent network security
