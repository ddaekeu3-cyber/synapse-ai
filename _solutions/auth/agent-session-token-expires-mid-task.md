---
layout: solution
title: "Agent Session Token Expires Mid-Task — Silent 401 Failures"
category: auth
description: "Agent starts a long multi-step task. OAuth access token expires after 1 hour. Agent silently gets 401 Unauthorized. Task fails mid-way. Agent has no token refresh logic. User must restart from scratch."
tags: [auth, oauth, token, refresh, expiry, 401, session, credentials, mid-task]
---

# Agent Session Token Expires Mid-Task — Silent 401 Failures

## Symptom
- Agent runs for 45 minutes then suddenly gets 401 Unauthorized errors
- Task progress is lost — no recovery, user must start over
- Error message: `{"error": "token_expired"}` but agent treats it as a generic failure
- Agent retries the same request 3 times, all fail with 401, then gives up
- Multi-step task that takes 2 hours fails reliably after the first hour
- Access token was valid at task start but OAuth tokens expire in 3600 seconds

## Root Cause
OAuth access tokens are short-lived by design — typically 1 hour. Long-running agent tasks that acquire a token at startup and never refresh it will eventually receive 401 errors. Most retry logic doesn't distinguish between transient 5xx errors (retry) and auth failures (refresh token, then retry). Without token refresh logic, the agent cannot recover from expiry. The refresh token, which has a much longer TTL, is often available but unused.

## Fix

### Option 1: Proactive token refresh before expiry

```python
import time
import httpx
import os
from dataclasses import dataclass

@dataclass
class OAuthToken:
    access_token: str
    refresh_token: str
    expires_at: float  # Unix timestamp
    token_type: str = "Bearer"

    @property
    def is_expired(self) -> bool:
        return time.time() >= self.expires_at

    @property
    def expires_in(self) -> float:
        return max(0.0, self.expires_at - time.time())

    @property
    def should_refresh(self) -> bool:
        """Refresh when less than 5 minutes remain"""
        return self.expires_in < 300

class TokenManager:
    """
    Manages OAuth tokens with automatic proactive refresh.
    Refreshes before expiry — never lets the token expire during active use.
    """

    def __init__(self, token_url: str, client_id: str, client_secret: str):
        self.token_url = token_url
        self.client_id = client_id
        self.client_secret = client_secret
        self._token: OAuthToken | None = None
        self._refreshing = False

    def set_initial_token(
        self, access_token: str, refresh_token: str, expires_in: int
    ):
        """Call this after initial OAuth flow"""
        self._token = OAuthToken(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=time.time() + expires_in
        )
        print(f"Token set — expires in {expires_in}s ({expires_in//60}m)")

    async def get_valid_token(self) -> str:
        """
        Get a valid access token — refreshes automatically if needed.
        Call this before every API request instead of caching the token string.
        """
        if self._token is None:
            raise RuntimeError("No token set — call set_initial_token() first")

        if self._token.should_refresh:
            print(f"Token expiring in {self._token.expires_in:.0f}s — refreshing proactively")
            await self._refresh()

        return self._token.access_token

    async def _refresh(self):
        """Exchange refresh token for new access token"""
        if self._refreshing:
            # Prevent concurrent refresh
            while self._refreshing:
                await asyncio.sleep(0.1)
            return

        self._refreshing = True
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    self.token_url,
                    data={
                        "grant_type": "refresh_token",
                        "refresh_token": self._token.refresh_token,
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                    },
                    timeout=30.0
                )
                response.raise_for_status()
                data = response.json()

            self._token = OAuthToken(
                access_token=data["access_token"],
                refresh_token=data.get("refresh_token", self._token.refresh_token),
                expires_at=time.time() + data["expires_in"]
            )
            print(f"Token refreshed — new expiry in {data['expires_in']}s")
        finally:
            self._refreshing = False

import asyncio

# Usage in agent:
token_mgr = TokenManager(
    token_url="https://auth.example.com/oauth/token",
    client_id=os.environ["CLIENT_ID"],
    client_secret=os.environ["CLIENT_SECRET"]
)

async def call_api(endpoint: str, payload: dict) -> dict:
    token = await token_mgr.get_valid_token()  # Always fresh
    async with httpx.AsyncClient() as client:
        response = await client.post(
            endpoint,
            headers={"Authorization": f"Bearer {token}"},
            json=payload,
            timeout=30.0
        )
        response.raise_for_status()
        return response.json()
```

### Option 2: Retry with token refresh on 401

```python
import httpx
import asyncio
from typing import Callable, Awaitable

async def request_with_token_refresh(
    make_request: Callable[[], Awaitable[httpx.Response]],
    token_manager: TokenManager,
    max_auth_retries: int = 2
) -> httpx.Response:
    """
    Execute HTTP request, refreshing token and retrying on 401.
    Separates auth failures (refresh + retry) from other errors.
    """
    for attempt in range(max_auth_retries + 1):
        response = await make_request()

        if response.status_code == 401:
            if attempt >= max_auth_retries:
                raise RuntimeError(
                    f"Authentication failed after {max_auth_retries} token refresh attempts. "
                    f"Refresh token may be expired — re-authentication required."
                )
            print(f"401 Unauthorized — refreshing token (attempt {attempt + 1})")
            await token_manager._refresh()
            continue  # Retry with new token

        if response.status_code == 403:
            # 403 is permissions, not token expiry — don't retry
            raise PermissionError(
                f"403 Forbidden — token is valid but lacks required permissions: {response.text}"
            )

        response.raise_for_status()
        return response

    raise RuntimeError("Max auth retries exceeded")

# Usage:
async def get_user_data(user_id: str) -> dict:
    async with httpx.AsyncClient() as client:
        async def make_request():
            token = await token_manager.get_valid_token()
            return await client.get(
                f"https://api.example.com/users/{user_id}",
                headers={"Authorization": f"Bearer {token}"},
                timeout=30.0
            )

        response = await request_with_token_refresh(make_request, token_manager)
        return response.json()
```

### Option 3: Persist tokens across agent restarts

```python
import json
import os
import time
from pathlib import Path

class PersistentTokenStore:
    """
    Save OAuth tokens to disk — survive agent restarts.
    Long-running tasks can resume after crash with valid tokens.
    """

    def __init__(self, token_file: str = "/data/oauth_tokens.json"):
        self.path = Path(token_file)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def save(self, service: str, token_data: dict):
        """Save token data for a named service"""
        tokens = self._load_all()
        tokens[service] = {
            **token_data,
            "saved_at": time.time()
        }
        # Atomic write
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(tokens, indent=2))
        tmp.replace(self.path)
        print(f"Token saved for {service} (expires in {token_data.get('expires_in', '?')}s)")

    def load(self, service: str) -> dict | None:
        """Load token data for a service"""
        tokens = self._load_all()
        data = tokens.get(service)
        if not data:
            return None

        # Reconstruct with adjusted expiry
        elapsed = time.time() - data["saved_at"]
        original_expires_in = data.get("expires_in", 3600)
        remaining = original_expires_in - elapsed

        if remaining <= 0:
            print(f"Stored token for {service} is expired — need refresh")
            data["is_expired"] = True
        else:
            data["remaining_seconds"] = remaining
            print(f"Loaded token for {service} — {remaining:.0f}s remaining")

        return data

    def _load_all(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text())
        except Exception:
            return {}

    def delete(self, service: str):
        tokens = self._load_all()
        tokens.pop(service, None)
        self.path.write_text(json.dumps(tokens, indent=2))

store = PersistentTokenStore()

# On agent startup — try to load existing token
def initialize_token_manager(service: str) -> TokenManager:
    mgr = TokenManager(
        token_url=os.environ["TOKEN_URL"],
        client_id=os.environ["CLIENT_ID"],
        client_secret=os.environ["CLIENT_SECRET"]
    )

    saved = store.load(service)
    if saved and not saved.get("is_expired"):
        # Restore from disk
        mgr.set_initial_token(
            access_token=saved["access_token"],
            refresh_token=saved["refresh_token"],
            expires_in=int(saved.get("remaining_seconds", 60))
        )
    else:
        # Need fresh auth — but refresh token may still work
        if saved and saved.get("refresh_token"):
            print(f"Access token expired but refresh token available — refreshing")
            mgr._token = OAuthToken(
                access_token="",
                refresh_token=saved["refresh_token"],
                expires_at=0  # Force refresh on next get_valid_token()
            )

    return mgr
```

### Option 4: Background token refresh loop

```python
import asyncio
import time

async def token_refresh_loop(token_mgr: TokenManager, check_interval: float = 60.0):
    """
    Background task that keeps the token fresh continuously.
    Start this as an asyncio task at agent startup.
    """
    print(f"Token refresh loop started (checks every {check_interval}s)")

    while True:
        await asyncio.sleep(check_interval)

        if token_mgr._token is None:
            continue

        expires_in = token_mgr._token.expires_in

        if expires_in < 600:  # Refresh when less than 10 minutes remain
            print(f"Proactive refresh — token expires in {expires_in:.0f}s")
            try:
                await token_mgr._refresh()
                print(f"Token refreshed successfully")
            except Exception as e:
                print(f"Token refresh failed: {e} — will retry next cycle")
        else:
            print(f"Token healthy — {expires_in:.0f}s remaining ({expires_in//60:.0f}m)")

# At agent startup:
async def run_agent():
    token_mgr = initialize_token_manager("my_service")

    # Start background refresh loop
    refresh_task = asyncio.create_task(token_refresh_loop(token_mgr))

    try:
        # Run long agent task — token stays fresh automatically
        await run_long_agent_task(token_mgr)
    finally:
        refresh_task.cancel()
        try:
            await refresh_task
        except asyncio.CancelledError:
            pass
```

### Option 5: Detect and handle token-related error codes

```python
from enum import Enum

class AuthErrorType(Enum):
    TOKEN_EXPIRED = "token_expired"
    TOKEN_INVALID = "token_invalid"
    INSUFFICIENT_SCOPE = "insufficient_scope"
    REFRESH_TOKEN_EXPIRED = "refresh_token_expired"
    UNKNOWN = "unknown"

def classify_auth_error(response: httpx.Response) -> AuthErrorType:
    """
    Classify a 401/403 response to determine recovery strategy.
    """
    try:
        body = response.json()
    except Exception:
        body = {}

    error = body.get("error", "").lower()
    description = body.get("error_description", "").lower()
    www_auth = response.headers.get("www-authenticate", "").lower()

    # Token expired — can recover with refresh
    if any(x in error + description + www_auth for x in
           ["expired", "token_expired", "access_token_expired"]):
        return AuthErrorType.TOKEN_EXPIRED

    # Refresh token expired — need full re-auth
    if any(x in error + description for x in
           ["refresh_token_expired", "invalid_grant", "refresh_expired"]):
        return AuthErrorType.REFRESH_TOKEN_EXPIRED

    # Wrong scope — need re-auth with different permissions
    if any(x in error + description + www_auth for x in
           ["insufficient_scope", "forbidden", "scope"]):
        return AuthErrorType.INSUFFICIENT_SCOPE

    # Invalid token — tampering or format issue
    if any(x in error + description for x in
           ["invalid_token", "malformed", "signature"]):
        return AuthErrorType.TOKEN_INVALID

    return AuthErrorType.UNKNOWN

async def handle_auth_error(
    error_type: AuthErrorType,
    token_mgr: TokenManager
) -> bool:
    """
    Attempt recovery from auth error.
    Returns True if recovery succeeded (caller should retry request).
    """
    match error_type:
        case AuthErrorType.TOKEN_EXPIRED:
            print("Access token expired — refreshing")
            await token_mgr._refresh()
            return True  # Retry

        case AuthErrorType.REFRESH_TOKEN_EXPIRED:
            print("Refresh token expired — full re-authentication required")
            # Notify user or trigger OAuth flow
            raise RuntimeError(
                "Session expired. Please re-authenticate. "
                "Task progress has been saved — you can resume after login."
            )

        case AuthErrorType.INSUFFICIENT_SCOPE:
            print("Token lacks required scope — cannot recover automatically")
            raise PermissionError(
                "Insufficient permissions. Re-authorize with the required scopes."
            )

        case _:
            print(f"Unknown auth error — cannot recover")
            return False
```

### Option 6: Task checkpointing before token-sensitive operations

```python
import json
from pathlib import Path

class TokenAwareTaskRunner:
    """
    Run long tasks with checkpointing.
    If token expires and cannot be refreshed, save progress and exit cleanly.
    User can resume after re-authentication.
    """

    def __init__(self, task_id: str, token_mgr: TokenManager):
        self.task_id = task_id
        self.token_mgr = token_mgr
        self.checkpoint_path = Path(f"/data/checkpoints/{task_id}.json")
        self.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        self._progress = self._load_checkpoint()

    def _load_checkpoint(self) -> dict:
        if self.checkpoint_path.exists():
            data = json.loads(self.checkpoint_path.read_text())
            print(f"Resuming from checkpoint: step {data.get('current_step')}")
            return data
        return {"current_step": 0, "completed": [], "results": {}}

    def save_checkpoint(self, step: int, result_key: str, result_value):
        self._progress["current_step"] = step
        self._progress["completed"].append(result_key)
        self._progress["results"][result_key] = result_value
        tmp = self.checkpoint_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._progress, indent=2))
        tmp.replace(self.checkpoint_path)

    async def run_step(self, step: int, step_name: str, work_fn) -> any:
        """Run a step — skip if already completed, save progress after"""
        if step_name in self._progress["completed"]:
            print(f"Step {step} ({step_name}): already done — skipping")
            return self._progress["results"].get(step_name)

        # Check token is valid before expensive step
        try:
            await self.token_mgr.get_valid_token()
        except Exception as e:
            raise RuntimeError(
                f"Token refresh failed before step {step} ({step_name}). "
                f"Progress saved at step {self._progress['current_step']}. "
                f"Re-authenticate and resume task {self.task_id}."
            ) from e

        result = await work_fn()
        self.save_checkpoint(step, step_name, result)
        print(f"Step {step} ({step_name}): complete")
        return result
```

## Token Refresh Strategy by OAuth Flow

| Grant Type | Access Token TTL | Refresh Token TTL | Recovery Strategy |
|---|---|---|---|
| Authorization Code | 1 hour | Days–months | Refresh token flow |
| Client Credentials | 1 hour | N/A (no refresh) | Re-issue new token |
| Device Code | 1 hour | Days | Refresh token flow |
| PKCE | 1 hour | Days | Refresh token flow |
| API Key (not OAuth) | Unlimited | N/A | Rotation schedule |

## Expected Token Savings
Long task fails at 75% due to token expiry → restart from scratch: ~40,000 tokens
Proactive refresh + checkpoint → task completes in one run: 0 wasted tokens

## Environment
- Any agent using OAuth-based APIs for long-running tasks; critical for agents accessing Google APIs, GitHub, Slack, Salesforce, and any service with short-lived access tokens
- Source: direct experience; token expiry mid-task is the most common auth failure mode for agents running batch jobs or multi-step workflows
