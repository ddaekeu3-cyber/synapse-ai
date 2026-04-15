---
layout: solution
title: "API Tool Returns HTML Error Page Instead of JSON — JSONDecodeError on Error Response"
category: tool-failure
description: "Agent calls an API expecting JSON. API returns an HTML error page (500, maintenance page, or WAF block). json.loads() fails with JSONDecodeError. Agent doesn't handle non-JSON responses."
tags: [json, html, api, error-response, parsing, tool-failure, waf]
---

# API Tool Returns HTML Error Page Instead of JSON — JSONDecodeError on Error Response

## Symptom
- `json.JSONDecodeError: Expecting value: line 1 column 1 (char 0)`
- `json.decoder.JSONDecodeError: Extra data: line 2 column 1`
- Response starts with `<!DOCTYPE html>` or `<html>`
- Works during normal operation, fails during maintenance or high load
- WAF (Web Application Firewall) returns HTML block page instead of JSON
- CDN returns HTML error page on backend failure

## Root Cause
HTTP servers can return HTML for any status code — including 500, 503, and even 200 (maintenance pages). If your code unconditionally calls `response.json()` without checking `Content-Type`, an HTML response causes a JSON parse failure. The actual error (maintenance, auth block, WAF) is hidden behind the JSON parse exception.

## Fix

### Option 1: Check Content-Type before parsing

```python
import httpx

async def safe_api_call(url: str, **kwargs) -> dict:
    async with httpx.AsyncClient() as client:
        response = await client.get(url, **kwargs)

    content_type = response.headers.get("content-type", "")

    if "application/json" not in content_type:
        # Not JSON — inspect what we actually got
        body_preview = response.text[:500]
        raise ValueError(
            f"Expected JSON but got {content_type}\n"
            f"Status: {response.status_code}\n"
            f"Body preview: {body_preview}"
        )

    response.raise_for_status()
    return response.json()
```

### Option 2: Try JSON parse, fall back to informative error

```python
import json, httpx

def parse_response(response: httpx.Response) -> dict:
    """Parse response with helpful error on non-JSON content"""
    raw = response.text

    # Try JSON first
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # Detect HTML error pages
    if raw.strip().startswith("<!") or raw.strip().startswith("<html"):
        if response.status_code == 503:
            raise ServiceUnavailableError("API is in maintenance mode (returned HTML 503 page)")
        elif response.status_code == 403:
            raise AuthError("Request blocked (returned HTML 403 — likely WAF or IP block)")
        elif response.status_code == 500:
            raise APIError(f"API internal error (returned HTML 500 page). Body: {raw[:200]}")
        else:
            raise APIError(
                f"API returned HTML instead of JSON (status {response.status_code}). "
                f"This usually means maintenance, WAF block, or misconfigured endpoint. "
                f"Body preview: {raw[:300]}"
            )

    # Non-JSON, non-HTML
    raise APIError(f"Unexpected response format (status {response.status_code}): {raw[:200]}")
```

### Option 3: Robust request wrapper with full diagnosis

```python
import httpx, json

class RobustAPIClient:
    def __init__(self, base_url: str, headers: dict = None):
        self.base_url = base_url
        self.default_headers = {"Accept": "application/json", **(headers or {})}

    async def get(self, path: str, **kwargs) -> dict:
        return await self._request("GET", path, **kwargs)

    async def post(self, path: str, **kwargs) -> dict:
        return await self._request("POST", path, **kwargs)

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        url = f"{self.base_url}{path}"
        headers = {**self.default_headers, **kwargs.pop("headers", {})}

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.request(method, url, headers=headers, **kwargs)

        # Log non-200 for debugging
        if response.status_code >= 400:
            print(f"API error: {method} {url} → {response.status_code}")

        # Check content type
        content_type = response.headers.get("content-type", "")
        if "application/json" not in content_type and "text/json" not in content_type:
            body = response.text[:1000]
            raise APIResponseError(
                f"Non-JSON response from {url}: "
                f"status={response.status_code}, "
                f"content-type={content_type!r}, "
                f"body={body!r}"
            )

        try:
            data = response.json()
        except json.JSONDecodeError as e:
            raise APIResponseError(
                f"Invalid JSON from {url}: {e}\n"
                f"Raw response: {response.text[:500]}"
            ) from e

        response.raise_for_status()  # Raise for 4xx/5xx after JSON parse
        return data
```

### Option 4: Handle HTML responses gracefully in agent

```
System prompt:
"When calling API tools:
1. Always check if the response is valid JSON before processing
2. If response starts with '<' or '<!DOCTYPE', it's an HTML error page — report the HTTP status code
3. Common causes of HTML responses:
   - 503: API maintenance, retry in 5 minutes
   - 403: IP blocked or authentication issue
   - 500: API internal error, retry once
   - 429: Rate limited, wait and retry
4. Never crash on a non-JSON API response — report what you received"
```

### Option 5: Validate API endpoint is returning JSON during setup

```python
async def validate_api_endpoint(url: str, api_key: str = None):
    """Health check that validates JSON response format"""
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(url, headers=headers, timeout=10.0)
        except httpx.ConnectError:
            raise RuntimeError(f"Cannot connect to {url}")

    content_type = response.headers.get("content-type", "")
    if "json" not in content_type:
        raise RuntimeError(
            f"API endpoint {url} is not returning JSON. "
            f"Got Content-Type: {content_type}. "
            f"Response: {response.text[:300]}"
        )

    print(f"API endpoint {url} validated: returns JSON (status {response.status_code})")

# Run at startup
await validate_api_endpoint("https://api.example.com/health")
```

## Common HTML-Instead-of-JSON Scenarios

| Scenario | Status | Body | Fix |
|---|---|---|---|
| Maintenance page | 200 | HTML page | Wait, retry later |
| WAF block | 403 | HTML block page | Check IP, headers |
| Load balancer error | 502/504 | HTML error | Retry with backoff |
| SSL termination error | 200 | HTML redirect | Check HTTPS config |
| Wrong endpoint URL | 404 | HTML 404 page | Fix URL |
| CDN caching HTML error | 200 | Cached HTML | Add Cache-Control headers |

## Expected Token Savings
Debugging JSONDecodeError on HTML response: ~4,000 tokens
Content-Type check prevents confusion: immediate clear error message

## Environment
- Any agent calling external APIs, especially during traffic spikes or incidents
- Source: direct experience; extremely common when APIs go behind CDNs or WAFs
