---
layout: solution
title: "Agent Uses Deprecated API Version — Endpoints Return 410 Gone or Changed Behavior"
category: general
description: "Agent was built against API v1, which is now deprecated. Requests to old endpoints return 410 Gone or redirect to v2. Agent continues using old version, breaking silently or with confusing errors."
tags: [api-version, deprecated, 410, migration, api-client, versioning]
---

# Agent Uses Deprecated API Version — Endpoints Return 410 Gone or Changed Behavior

## Symptom
- HTTP 410 Gone from previously working endpoint
- Response format changed unexpectedly — field names different, structure altered
- `{"error": "This API version is deprecated. Please use /v2/"}` responses
- Agent suddenly fails after months of working correctly
- Warning emails from API provider about deprecation that went unnoticed

## Root Cause
API providers evolve their APIs and deprecate older versions. Agents hardcode API endpoint URLs or version strings and don't monitor for deprecation warnings. When the old version is sunset, all calls fail simultaneously.

## Fix

### Option 1: Pin API version explicitly and monitor deprecation headers

```python
import httpx, logging

logger = logging.getLogger(__name__)

class VersionedAPIClient:
    def __init__(self, base_url: str, api_version: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self.api_version = api_version
        self.api_key = api_key

    def _check_deprecation_headers(self, response: httpx.Response):
        """Log any deprecation warnings from API headers"""
        deprecation_headers = {
            "Deprecation": "API feature is deprecated",
            "Sunset": "API will be removed after this date",
            "X-API-Warn": "API warning",
            "Warning": "General HTTP warning",
        }
        for header, meaning in deprecation_headers.items():
            value = response.headers.get(header)
            if value:
                logger.warning(
                    f"API DEPRECATION NOTICE — {meaning}: {value}\n"
                    f"Endpoint: {response.url}\n"
                    f"Action required: update API version or endpoint"
                )

    async def get(self, path: str, **kwargs) -> dict:
        url = f"{self.base_url}/{self.api_version}/{path.lstrip('/')}"
        async with httpx.AsyncClient() as client:
            response = await client.get(
                url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Accept": "application/json",
                },
                **kwargs
            )
            self._check_deprecation_headers(response)
            response.raise_for_status()
            return response.json()

# Pin the version explicitly — easy to update in one place
client = VersionedAPIClient(
    base_url="https://api.example.com",
    api_version="v2",  # Update this when v3 comes out
    api_key=os.environ["API_KEY"]
)
```

### Option 2: Centralize all API URLs in one config file

```python
# api_config.py — update here when API versions change
API_ENDPOINTS = {
    "anthropic": {
        "base_url": "https://api.anthropic.com",
        "version": "v1",  # Current as of 2025
        "messages": "/v1/messages",
        "count_tokens": "/v1/messages/count_tokens",
    },
    "openai": {
        "base_url": "https://api.openai.com",
        "version": "v1",
        "chat": "/v1/chat/completions",
    },
    "github": {
        "base_url": "https://api.github.com",
        "version": "2022-11-28",  # GitHub uses date versioning in header
        "repos": "/repos",
        "pulls": "/pulls",
    }
}

def get_endpoint(service: str, endpoint: str) -> str:
    config = API_ENDPOINTS[service]
    return config["base_url"] + config[endpoint]

# Never hardcode URLs inline — always use this function
url = get_endpoint("anthropic", "messages")
```

### Option 3: Handle version negotiation gracefully

```python
import httpx

SUPPORTED_VERSIONS = ["v3", "v2", "v1"]  # Try newest first

async def call_with_version_fallback(endpoint_template: str, **kwargs) -> dict:
    """Try API versions newest-first, fall back on 410/404"""
    async with httpx.AsyncClient() as client:
        for version in SUPPORTED_VERSIONS:
            url = endpoint_template.format(version=version)
            response = await client.get(url, **kwargs)

            if response.status_code == 410:
                print(f"Version {version} is deprecated (410). Trying older version...")
                continue
            elif response.status_code == 404:
                continue
            elif response.status_code == 200:
                if version != SUPPORTED_VERSIONS[0]:
                    logger.warning(
                        f"Using API version {version} — newer versions may be available"
                    )
                return response.json()
            else:
                response.raise_for_status()

    raise RuntimeError(f"No working API version found from: {SUPPORTED_VERSIONS}")

# Usage
data = await call_with_version_fallback(
    "https://api.example.com/{version}/users",
    headers={"Authorization": f"Bearer {API_KEY}"}
)
```

### Option 4: Monitor for deprecation proactively

```python
import httpx, os
from datetime import datetime

DEPRECATION_MONITOR_ENDPOINTS = [
    "https://api.example.com/v1/health",
    "https://api.example.com/v1/users",
]

async def check_api_versions():
    """Run daily — check for deprecation headers before they cause problems"""
    issues = []
    async with httpx.AsyncClient() as client:
        for url in DEPRECATION_MONITOR_ENDPOINTS:
            response = await client.get(url, headers={"Authorization": f"Bearer {API_KEY}"})
            sunset = response.headers.get("Sunset")
            deprecation = response.headers.get("Deprecation")

            if sunset:
                sunset_date = datetime.fromisoformat(sunset)
                days_until = (sunset_date - datetime.now()).days
                issues.append(
                    f"SUNSET IN {days_until} DAYS: {url}\n"
                    f"  Sunset date: {sunset}\n"
                    f"  Action: migrate to new API version before {sunset}"
                )
            if deprecation:
                issues.append(f"DEPRECATED: {url} — {deprecation}")

    if issues:
        alert_team("\n\n".join(issues))

# Schedule this to run daily (cron, APScheduler, etc.)
```

### Option 5: Anthropic SDK — always use current model IDs

```python
# Anthropic doesn't version the Messages API endpoint
# but model IDs change — use current IDs

# WRONG — these model IDs are deprecated
OLD_MODELS = [
    "claude-2",
    "claude-instant-1",
    "claude-v1",
    "claude-3-opus-20240229",  # Not deprecated, but newer versions exist
]

# RIGHT — use current model IDs (2025)
CURRENT_MODELS = {
    "fast": "claude-haiku-4-5-20251001",
    "balanced": "claude-sonnet-4-6",
    "powerful": "claude-opus-4-6",
}

import anthropic
client = anthropic.Anthropic()

# Use SDK defaults when possible — SDK ships with current defaults
response = client.messages.create(
    model=CURRENT_MODELS["balanced"],
    max_tokens=1024,
    messages=[{"role": "user", "content": "Hello"}]
)
```

## API Version Migration Checklist

| Step | Action |
|---|---|
| 1 | Subscribe to provider's API changelog / release notes |
| 2 | Add deprecation header logging to all API clients |
| 3 | Centralize all API URLs in one config file |
| 4 | Run daily health check for Sunset headers |
| 5 | Test against new API version in staging before forced migration |
| 6 | Update version string in config when ready |

## Expected Token Savings
Debugging 410 errors without understanding API versioning: ~5,000 tokens
Centralized API config + deprecation monitoring: 0 wasted

## Environment
- Any agent integrating with third-party APIs; especially long-running production deployments
- Source: direct experience with API deprecations; GitHub, Stripe, and others follow this pattern
