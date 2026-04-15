---
layout: solution
title: "JWT Token Expires Mid-Session — Silent 401 Errors After 1 Hour"
category: auth
description: "Agent works fine at session start but starts getting 401 errors after 60 minutes. JWT access token expired. Agent doesn't detect the pattern and keeps retrying with invalid token."
tags: [jwt, 401, token-expiry, auth-refresh, session]
---

# JWT Token Expires Mid-Session — Silent 401 Errors After 1 Hour

## Symptom
- Agent works for ~60 minutes then starts failing
- API calls return 401 Unauthorized
- Error message: `{"error": "token_expired"}` or `{"error": "invalid_token"}`
- Agent retries same call multiple times — all fail with same 401
- Session started fine; nothing changed except time
- Problem always appears at the same interval (usually 1 hour or 24 hours)

## Root Cause
JWT access tokens have short expiry (typically 1 hour). When the token expires, all API calls using it get 401. Without automatic token refresh, the agent is stuck with an expired credential for the remainder of the session.

## Fix

### Option 1: Automatic refresh on 401

```python
import httpx, time
from functools import wraps

class AuthenticatedClient:
    def __init__(self, access_token, refresh_token, token_url):
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.token_url = token_url
        self.token_expires_at = time.time() + 3600  # Default 1hr

    def _is_expired(self):
        return time.time() >= self.token_expires_at - 60  # Refresh 60s early

    async def refresh(self):
        async with httpx.AsyncClient() as client:
            resp = await client.post(self.token_url, json={
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token
            })
            resp.raise_for_status()
            data = resp.json()
            self.access_token = data["access_token"]
            self.token_expires_at = time.time() + data.get("expires_in", 3600)

    async def request(self, method, url, **kwargs):
        if self._is_expired():
            await self.refresh()

        kwargs.setdefault("headers", {})["Authorization"] = f"Bearer {self.access_token}"
        async with httpx.AsyncClient() as client:
            response = await client.request(method, url, **kwargs)

        if response.status_code == 401:
            # Token may have expired between check and use — refresh and retry once
            await self.refresh()
            kwargs["headers"]["Authorization"] = f"Bearer {self.access_token}"
            async with httpx.AsyncClient() as client:
                response = await client.request(method, url, **kwargs)

        return response
```

### Option 2: Token expiry detection from JWT payload

```python
import base64, json, time

def get_jwt_expiry(token):
    """Decode JWT payload without verification to get exp claim"""
    payload_b64 = token.split('.')[1]
    # Add padding if needed
    payload_b64 += '=' * (4 - len(payload_b64) % 4)
    payload = json.loads(base64.urlsafe_b64decode(payload_b64))
    return payload.get('exp', 0)

def is_token_valid(token, buffer_seconds=60):
    """Return True if token is valid for at least buffer_seconds more"""
    exp = get_jwt_expiry(token)
    return time.time() < exp - buffer_seconds
```

### Option 3: Proactive refresh before expiry

```python
import asyncio

async def token_refresh_loop(client, interval_seconds=1800):
    """Refresh token every 30 minutes (before 1hr expiry)"""
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await client.refresh()
            print("Token refreshed proactively")
        except Exception as e:
            print(f"Token refresh failed: {e}")
            # Alert here — session will expire soon

# Start in background
asyncio.create_task(token_refresh_loop(client))
```

### Option 4: OpenClaw config — auto token refresh

```yaml
# openclaw.config.yaml
auth:
  jwt:
    auto_refresh: true
    refresh_buffer_seconds: 300    # Refresh 5 minutes before expiry
    on_refresh_failure: retry_with_reauth
  oauth:
    token_url: ${OAUTH_TOKEN_URL}
    client_id: ${OAUTH_CLIENT_ID}
    client_secret: ${OAUTH_CLIENT_SECRET}
```

## Detection Pattern

```python
def detect_token_expiry_pattern(error_log):
    """Check if 401s correlate with session duration"""
    import re
    from datetime import datetime

    first_request = None
    errors = []

    for line in error_log:
        if "401" in line or "token_expired" in line:
            # Extract timestamp and check if ~1hr after session start
            timestamp = extract_timestamp(line)
            if first_request and (timestamp - first_request).seconds > 3000:
                errors.append(timestamp)

    return len(errors) > 0, f"Token expiry pattern detected — {len(errors)} 401s after session hour"
```

## Expected Token Savings
Agent stuck in 401 retry loop for 30 minutes: ~40,000 tokens wasted
Proactive refresh: 0 extra tokens, seamless

## Environment
- Any agent using JWT-based authentication
- Most common: OAuth2 access tokens (Google, GitHub, Slack, etc.)
- Source: direct experience, standard JWT expiry pattern
