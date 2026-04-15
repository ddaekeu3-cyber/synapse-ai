---
layout: solution
title: "Agent Uses Expired OAuth Token Without Refreshing — Silent 401 After Hours"
category: auth
description: "Agent authenticates at startup with a valid OAuth token. After 1 hour the token expires. Agent continues using it, gets 401s, and fails silently or crashes rather than refreshing."
tags: [oauth, token-refresh, 401, authentication, expiry, access-token]
---

# Agent Uses Expired OAuth Token Without Refreshing — Silent 401 After Hours

## Symptom
- Agent works perfectly for the first hour, then starts getting 401 errors
- Restarting the agent fixes the problem temporarily
- Error: `{"error": "invalid_token", "error_description": "The access token expired"}`
- Long-running agents fail after exactly 1 hour (standard OAuth access token TTL)
- Agent has a refresh token but never uses it

## Root Cause
OAuth access tokens typically expire after 1 hour. Refresh tokens last days/weeks but require a separate API call to exchange for a new access token. Agents that store the access token at startup and never check expiry will silently fail when the token expires.

## Fix

### Option 1: Check token expiry before every request

```python
import time
from dataclasses import dataclass

@dataclass
class OAuthToken:
    access_token: str
    refresh_token: str
    expires_at: float  # Unix timestamp

    def is_expired(self, buffer_seconds: int = 60) -> bool:
        """Consider token expired 60s before actual expiry for safety margin"""
        return time.time() > (self.expires_at - buffer_seconds)

class OAuthSession:
    def __init__(self, client_id: str, client_secret: str, token_url: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.token_url = token_url
        self._token: OAuthToken | None = None

    def get_access_token(self) -> str:
        if self._token is None or self._token.is_expired():
            self._token = self._refresh()
        return self._token.access_token

    def _refresh(self) -> OAuthToken:
        import httpx
        response = httpx.post(self.token_url, data={
            "grant_type": "refresh_token",
            "refresh_token": self._token.refresh_token if self._token else None,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        })
        response.raise_for_status()
        data = response.json()
        return OAuthToken(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token", self._token.refresh_token),
            expires_at=time.time() + data.get("expires_in", 3600)
        )

    def get_headers(self) -> dict:
        return {"Authorization": f"Bearer {self.get_access_token()}"}
```

### Option 2: Retry on 401 with automatic refresh

```python
import httpx

async def api_request_with_refresh(
    method: str,
    url: str,
    oauth: OAuthSession,
    **kwargs
) -> httpx.Response:
    """Make API request, refresh token once on 401"""
    async with httpx.AsyncClient() as client:
        headers = {**kwargs.pop("headers", {}), **oauth.get_headers()}
        response = await client.request(method, url, headers=headers, **kwargs)

        if response.status_code == 401:
            # Token likely expired — force refresh and retry once
            oauth._token = None  # Clear cached token
            headers = {**kwargs.pop("headers", {}), **oauth.get_headers()}
            response = await client.request(method, url, headers=headers, **kwargs)

        return response
```

### Option 3: Background token refresh

```python
import asyncio, time

class AutoRefreshingOAuth:
    def __init__(self, initial_token: OAuthToken, refresh_fn):
        self._token = initial_token
        self._refresh_fn = refresh_fn
        self._lock = asyncio.Lock()
        self._refresh_task = None

    async def start(self):
        """Start background refresh loop"""
        self._refresh_task = asyncio.create_task(self._refresh_loop())

    async def stop(self):
        if self._refresh_task:
            self._refresh_task.cancel()

    async def _refresh_loop(self):
        while True:
            # Refresh at 80% of token lifetime
            time_until_expiry = self._token.expires_at - time.time()
            refresh_in = max(0, time_until_expiry * 0.8)
            await asyncio.sleep(refresh_in)

            async with self._lock:
                try:
                    self._token = await self._refresh_fn(self._token.refresh_token)
                    print(f"Token refreshed. Next expiry: {self._token.expires_at}")
                except Exception as e:
                    print(f"Token refresh failed: {e}. Retrying in 30s...")
                    await asyncio.sleep(30)

    async def get_token(self) -> str:
        async with self._lock:
            return self._token.access_token
```

### Option 4: Store token with expiry, persist across restarts

```python
import json
from pathlib import Path

TOKEN_FILE = Path.home() / ".agent_oauth_token.json"

def save_token(token: OAuthToken):
    TOKEN_FILE.write_text(json.dumps({
        "access_token": token.access_token,
        "refresh_token": token.refresh_token,
        "expires_at": token.expires_at
    }))

def load_token() -> OAuthToken | None:
    if not TOKEN_FILE.exists():
        return None
    data = json.loads(TOKEN_FILE.read_text())
    return OAuthToken(**data)

def get_valid_token(oauth: OAuthSession) -> str:
    """Load saved token, refresh if expired, save back"""
    token = load_token()
    if token and not token.is_expired():
        return token.access_token

    # Expired or missing — refresh
    new_token = oauth._refresh(token.refresh_token if token else None)
    save_token(new_token)
    return new_token.access_token
```

### Option 5: Google / AWS SDK pattern (automatic token management)

```python
# Most major SDKs handle token refresh automatically
# Prefer using SDKs over manual OAuth for well-supported APIs

# Google (google-auth handles refresh automatically)
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

creds = Credentials.from_authorized_user_file("token.json", SCOPES)
if creds.expired and creds.refresh_token:
    creds.refresh(Request())  # Automatically refreshes
    # Save updated token
    with open("token.json", "w") as f:
        f.write(creds.to_json())

# AWS (boto3 handles credential refresh automatically via STS)
import boto3
client = boto3.client("s3")  # Credentials refreshed automatically
```

## OAuth Token Lifetimes by Provider

| Provider | Access Token TTL | Refresh Token TTL |
|---|---|---|
| Google | 1 hour | 6 months (rolling) |
| GitHub | No expiry (classic) / 8 hours (fine-grained) | — |
| Slack | 12 hours | 30 days |
| Stripe | No expiry (API keys, not OAuth) | — |
| Anthropic | N/A (API keys) | — |
| Microsoft/Azure | 1 hour | 90 days |
| Twitter/X | 2 hours | 6 months |

## Expected Token Savings
Debugging mystery 401s after 1 hour: ~5,000 tokens
Proactive token refresh prevents them: 0 wasted

## Environment
- Any agent using OAuth for Google, GitHub, Slack, or other providers
- Source: direct experience with long-running agents hitting token expiry
