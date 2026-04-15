---
layout: solution
title: "Agent Fails Silently on Expired OAuth Tokens"
category: auth
description: "Agent receives a 401 Unauthorized error from an external API due to an expired OAuth token, but silently treats it as an empty result — hallucinating success or returning stale data without alerting the user."
tags: [auth, oauth, token-refresh, error-handling, reliability, api]
---

## Symptom

Agent calls a third-party API (Google, Slack, GitHub, etc.) and the token has expired. Instead of surfacing the error, the agent continues:

```
Tool result: []   ← empty, but actually a 401 Unauthorized
Agent: "No results found."  ← silently wrong
```

Or worse, the agent uses cached stale data from before the token expired, reporting outdated information as current fact.

## Root Cause

The tool wrapper catches all HTTP exceptions and returns empty results or `None`. The agent has no signal to distinguish "API returned empty" from "API rejected the request". Without token refresh logic, expired credentials cause silent degradation rather than a visible error.

## Fix

---

### Option 1 — Explicit Auth Error Propagation

Never swallow 401/403 errors. Raise a distinct `AuthError` that the agent can surface clearly to the user with an actionable message.

```python
import anthropic
import requests

class AuthError(Exception):
    def __init__(self, service: str, message: str):
        self.service = service
        super().__init__(f"[{service}] Auth error: {message}")

class TokenExpiredError(AuthError):
    pass

def call_api_with_auth_check(
    url: str,
    token: str,
    service_name: str,
    params: dict | None = None,
) -> dict:
    response = requests.get(
        url,
        headers={"Authorization": f"Bearer {token}"},
        params=params or {},
        timeout=10,
    )

    if response.status_code == 401:
        raise TokenExpiredError(
            service_name,
            "Access token is expired or revoked. Please re-authenticate."
        )
    if response.status_code == 403:
        raise AuthError(
            service_name,
            "Insufficient permissions. Check your OAuth scopes."
        )
    response.raise_for_status()
    return response.json()

def get_github_repos(token: str, username: str) -> list[dict]:
    return call_api_with_auth_check(
        url=f"https://api.github.com/users/{username}/repos",
        token=token,
        service_name="GitHub",
    )

# Tool wrapper that surfaces auth errors clearly
def github_repos_tool(token: str, username: str) -> str:
    try:
        repos = get_github_repos(token, username)
        return f"Found {len(repos)} repositories: {[r['name'] for r in repos[:5]]}"
    except TokenExpiredError as e:
        return (
            f"Authentication failed: {e}\n"
            "Action required: please refresh your GitHub OAuth token and try again."
        )
    except AuthError as e:
        return f"Authorization error: {e}"
    except requests.RequestException as e:
        return f"Network error: {e}"

client = anthropic.Anthropic()

TOOLS = [{
    "name": "get_github_repos",
    "description": "List GitHub repositories for a user. Returns an error if authentication fails.",
    "input_schema": {
        "type": "object",
        "properties": {
            "username": {"type": "string"},
            "token": {"type": "string"},
        },
        "required": ["username", "token"],
    },
}]

response = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=512,
    tools=TOOLS,
    messages=[{"role": "user", "content": "List repos for user octocat."}],
)

for block in response.content:
    if block.type == "tool_use" and block.name == "get_github_repos":
        result = github_repos_tool(
            token=block.input.get("token", "expired-token"),
            username=block.input["username"],
        )
        print(f"Tool result: {result}")
```

**Expected Token Savings:** None — correctness fix; prevents hallucinated "no results" responses
**Environment:** `pip install anthropic requests`

---

### Option 2 — Automatic Token Refresh with Retry

When a 401 occurs, automatically refresh the access token using the refresh token and retry the original request once. If refresh also fails, surface the error.

```python
import time
import requests
import anthropic
from dataclasses import dataclass

@dataclass
class OAuthCredentials:
    access_token: str
    refresh_token: str
    client_id: str
    client_secret: str
    token_url: str
    expires_at: float = 0.0

    def is_expired(self) -> bool:
        return time.time() >= self.expires_at - 60  # 60s buffer

def refresh_access_token(creds: OAuthCredentials) -> OAuthCredentials:
    """Exchange refresh token for new access token."""
    response = requests.post(
        creds.token_url,
        data={
            "grant_type": "refresh_token",
            "refresh_token": creds.refresh_token,
            "client_id": creds.client_id,
            "client_secret": creds.client_secret,
        },
        timeout=10,
    )

    if response.status_code != 200:
        raise RuntimeError(
            f"Token refresh failed ({response.status_code}): {response.text[:200]}"
        )

    data = response.json()
    creds.access_token = data["access_token"]
    creds.expires_at = time.time() + data.get("expires_in", 3600)
    if "refresh_token" in data:
        creds.refresh_token = data["refresh_token"]  # Rotate if provided

    return creds

def call_with_token_refresh(
    url: str,
    creds: OAuthCredentials,
    params: dict | None = None,
    max_refreshes: int = 1,
) -> tuple[dict, OAuthCredentials]:
    """
    Call an API endpoint, refreshing the token once on 401.
    Returns (response_data, updated_credentials).
    """
    for attempt in range(max_refreshes + 1):
        if creds.is_expired() or attempt > 0:
            print(f"[Token refresh attempt {attempt + 1}]")
            creds = refresh_access_token(creds)

        response = requests.get(
            url,
            headers={"Authorization": f"Bearer {creds.access_token}"},
            params=params or {},
            timeout=10,
        )

        if response.status_code == 401 and attempt < max_refreshes:
            print("[401 received — refreshing token]")
            continue

        if response.status_code == 401:
            raise RuntimeError("Authentication failed after token refresh. Please re-authorise.")

        response.raise_for_status()
        return response.json(), creds

    raise RuntimeError("Exhausted refresh attempts")

# Global credential store (in production: use a secure secrets manager)
_credentials: dict[str, OAuthCredentials] = {}

def get_user_data(user_id: str, service: str) -> str:
    creds = _credentials.get(f"{user_id}:{service}")
    if not creds:
        return f"No credentials found for {service}. Please authorise first."

    try:
        data, updated_creds = call_with_token_refresh(
            url=f"https://api.example-{service}.com/me",
            creds=creds,
        )
        _credentials[f"{user_id}:{service}"] = updated_creds
        return f"User data: {data}"
    except RuntimeError as e:
        return f"Authentication required: {e}"

# Demo with simulated expired credentials
_credentials["user-1:example"] = OAuthCredentials(
    access_token="expired-token",
    refresh_token="valid-refresh-token",
    client_id="app-client-id",
    client_secret="app-client-secret",
    token_url="https://auth.example.com/token",
    expires_at=time.time() - 1,  # Already expired
)

result = get_user_data("user-1", "example")
print(result)
```

**Expected Token Savings:** None — prevents failed tool calls from consuming unnecessary retry turns
**Environment:** `pip install anthropic requests`

---

### Option 3 — Proactive Token Expiry Check Before API Calls

Check token expiry before every API call. If the token will expire within a buffer window, refresh it proactively rather than waiting for a 401 failure.

```python
import time
import json
import sqlite3
import requests
from pathlib import Path
import anthropic

DB_PATH = Path("tokens.db")

def init_token_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS oauth_tokens (
            user_id TEXT,
            service TEXT,
            access_token TEXT,
            refresh_token TEXT,
            expires_at REAL,
            PRIMARY KEY (user_id, service)
        )
    """)
    conn.commit()
    conn.close()

def get_token(user_id: str, service: str) -> dict | None:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT access_token, refresh_token, expires_at FROM oauth_tokens WHERE user_id=? AND service=?",
        (user_id, service),
    ).fetchone()
    conn.close()
    if not row:
        return None
    return {"access_token": row[0], "refresh_token": row[1], "expires_at": row[2]}

def save_token(user_id: str, service: str, token_data: dict):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        INSERT INTO oauth_tokens (user_id, service, access_token, refresh_token, expires_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(user_id, service) DO UPDATE SET
            access_token=excluded.access_token,
            refresh_token=excluded.refresh_token,
            expires_at=excluded.expires_at
    """, (user_id, service, token_data["access_token"],
          token_data.get("refresh_token", ""), token_data["expires_at"]))
    conn.commit()
    conn.close()

REFRESH_BUFFER_SECONDS = 300  # Refresh 5 minutes before expiry

def get_valid_token(user_id: str, service: str, token_url: str, client_id: str, client_secret: str) -> str:
    token_data = get_token(user_id, service)

    if not token_data:
        raise RuntimeError(f"No token for {user_id}/{service}. Re-authorise.")

    if time.time() >= token_data["expires_at"] - REFRESH_BUFFER_SECONDS:
        print(f"[Proactive refresh for {user_id}/{service}]")
        response = requests.post(token_url, data={
            "grant_type": "refresh_token",
            "refresh_token": token_data["refresh_token"],
            "client_id": client_id,
            "client_secret": client_secret,
        }, timeout=10)

        if response.status_code != 200:
            raise RuntimeError(f"Refresh failed for {user_id}/{service}: {response.status_code}")

        new_data = response.json()
        updated = {
            "access_token": new_data["access_token"],
            "refresh_token": new_data.get("refresh_token", token_data["refresh_token"]),
            "expires_at": time.time() + new_data.get("expires_in", 3600),
        }
        save_token(user_id, service, updated)
        return updated["access_token"]

    return token_data["access_token"]

def call_api_with_proactive_refresh(user_id: str, service: str, url: str) -> dict:
    token = get_valid_token(
        user_id=user_id,
        service=service,
        token_url="https://oauth.example.com/token",
        client_id="my-client-id",
        client_secret="my-client-secret",
    )
    response = requests.get(
        url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )
    response.raise_for_status()
    return response.json()

# Demo
init_token_db()
save_token("user-1", "example", {
    "access_token": "soon-to-expire-token",
    "refresh_token": "valid-refresh-token",
    "expires_at": time.time() + 200,  # Expires in 200s, within 300s buffer
})

try:
    data = call_api_with_proactive_refresh("user-1", "example", "https://api.example.com/me")
    print(f"Data: {data}")
except Exception as e:
    print(f"Error: {e}")
```

**Expected Token Savings:** None — eliminates failed API calls caused by stale tokens
**Environment:** `pip install anthropic requests`

---

### Option 4 — Tool Result Auth Status Envelope

Wrap all tool results in a typed envelope with an `auth_status` field. The agent is instructed to check `auth_status` and stop if it is not `"ok"`.

```python
import json
import requests
import anthropic
from typing import Any

def make_tool_result(data: Any, auth_status: str = "ok", message: str = "") -> str:
    return json.dumps({
        "auth_status": auth_status,  # "ok" | "expired" | "forbidden" | "missing"
        "data": data,
        "message": message,
    })

def call_external_api(url: str, token: str) -> str:
    try:
        response = requests.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        if response.status_code == 401:
            return make_tool_result(
                None, "expired",
                "OAuth token expired. User must re-authenticate via /auth/refresh."
            )
        if response.status_code == 403:
            return make_tool_result(
                None, "forbidden",
                "Insufficient OAuth scopes. Required: read:user. User must re-authorise."
            )
        response.raise_for_status()
        return make_tool_result(response.json(), "ok")

    except requests.Timeout:
        return make_tool_result(None, "error", "API request timed out.")
    except requests.RequestException as e:
        return make_tool_result(None, "error", str(e))

client = anthropic.Anthropic()

TOOLS = [{
    "name": "fetch_user_profile",
    "description": (
        "Fetch a user's profile from the external API. "
        "Returns a JSON envelope with auth_status field. "
        "If auth_status is not 'ok', inform the user about the authentication issue "
        "and stop — do not proceed with the task."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "user_id": {"type": "string"},
            "token": {"type": "string"},
        },
        "required": ["user_id"],
    },
}]

messages = [{"role": "user", "content": "Show me my profile."}]

while True:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=(
            "You are a helpful assistant. When a tool returns auth_status != 'ok', "
            "immediately tell the user about the authentication issue and what they need to do. "
            "Do NOT proceed with the task or call other tools."
        ),
        tools=TOOLS,
        messages=messages,
    )

    if response.stop_reason == "end_turn":
        print("Agent:", next(b.text for b in response.content if b.type == "text"))
        break

    messages.append({"role": "assistant", "content": response.content})
    tool_results = []
    for block in response.content:
        if block.type == "tool_use":
            # Simulate expired token
            result = call_external_api(
                url=f"https://api.example.com/users/{block.input.get('user_id', 'me')}",
                token=block.input.get("token", "expired-token"),
            )
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": result,
            })
    messages.append({"role": "user", "content": tool_results})
```

**Expected Token Savings:** None — structured auth envelope prevents silent failure hallucinations
**Environment:** `pip install anthropic requests`

---

### Option 5 — OAuth Token Manager with Multi-Service Support

Centralised token manager handling refresh logic for multiple OAuth services. Tools retrieve tokens through the manager — never storing tokens in agent state.

```python
import time
import threading
import requests
import anthropic
from dataclasses import dataclass

@dataclass
class ServiceConfig:
    name: str
    token_url: str
    client_id: str
    client_secret: str

SERVICES: dict[str, ServiceConfig] = {
    "github": ServiceConfig("GitHub", "https://github.com/login/oauth/access_token", "gh-client-id", "gh-client-secret"),
    "slack": ServiceConfig("Slack", "https://slack.com/api/oauth.v2.access", "slack-client-id", "slack-client-secret"),
    "google": ServiceConfig("Google", "https://oauth2.googleapis.com/token", "google-client-id", "google-client-secret"),
}

class OAuthTokenManager:
    def __init__(self):
        self._tokens: dict[str, dict] = {}  # key: "user_id:service"
        self._lock = threading.Lock()

    def store(self, user_id: str, service: str, access_token: str, refresh_token: str, expires_in: int):
        with self._lock:
            self._tokens[f"{user_id}:{service}"] = {
                "access_token": access_token,
                "refresh_token": refresh_token,
                "expires_at": time.time() + expires_in,
            }

    def get_valid_token(self, user_id: str, service: str) -> str:
        key = f"{user_id}:{service}"
        with self._lock:
            token_data = self._tokens.get(key)

        if not token_data:
            raise RuntimeError(f"No {service} credentials for user {user_id}. Please connect your account.")

        if time.time() >= token_data["expires_at"] - 120:
            token_data = self._refresh(key, service, token_data)

        return token_data["access_token"]

    def _refresh(self, key: str, service: str, token_data: dict) -> dict:
        cfg = SERVICES.get(service)
        if not cfg:
            raise RuntimeError(f"Unknown service: {service}")

        print(f"[Token refresh: {service}]")
        try:
            response = requests.post(cfg.token_url, data={
                "grant_type": "refresh_token",
                "refresh_token": token_data["refresh_token"],
                "client_id": cfg.client_id,
                "client_secret": cfg.client_secret,
            }, timeout=10)
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as e:
            raise RuntimeError(f"{service} token refresh failed: {e}") from e

        updated = {
            "access_token": data["access_token"],
            "refresh_token": data.get("refresh_token", token_data["refresh_token"]),
            "expires_at": time.time() + data.get("expires_in", 3600),
        }
        with self._lock:
            self._tokens[key] = updated
        return updated

token_manager = OAuthTokenManager()

def build_oauth_tool(service: str, user_id: str):
    def fetch_data(endpoint: str) -> str:
        try:
            token = token_manager.get_valid_token(user_id, service)
            response = requests.get(
                endpoint,
                headers={"Authorization": f"Bearer {token}"},
                timeout=10,
            )
            response.raise_for_status()
            return str(response.json())
        except RuntimeError as e:
            return f"AUTH_ERROR: {e}"
        except requests.RequestException as e:
            return f"API_ERROR: {e}"
    return fetch_data

# Pre-store tokens (in production: populated during OAuth flow)
token_manager.store("user-1", "github", "gh-token", "gh-refresh", expires_in=100)
token_manager.store("user-1", "slack", "sl-token", "sl-refresh", expires_in=3600)

github_tool = build_oauth_tool("github", "user-1")
slack_tool = build_oauth_tool("slack", "user-1")

# Each tool transparently refreshes tokens as needed
print(github_tool("https://api.github.com/user"))
print(slack_tool("https://slack.com/api/auth.test"))
```

**Expected Token Savings:** None — centralised refresh eliminates duplicate refresh logic across tools
**Environment:** `pip install anthropic requests`

---

### Option 6 — Auth Health Check Before Long Agent Tasks

Before starting a multi-step agent task, run a lightweight auth health check across all required services. Surface failures before the task begins rather than mid-execution.

```python
import requests
import anthropic
from dataclasses import dataclass

@dataclass
class AuthCheckResult:
    service: str
    healthy: bool
    error: str = ""

def check_token(service: str, url: str, token: str) -> AuthCheckResult:
    try:
        response = requests.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=5,
        )
        if response.status_code == 200:
            return AuthCheckResult(service, True)
        if response.status_code == 401:
            return AuthCheckResult(service, False, "Token expired — please re-authenticate.")
        if response.status_code == 403:
            return AuthCheckResult(service, False, "Insufficient permissions — check OAuth scopes.")
        return AuthCheckResult(service, False, f"HTTP {response.status_code}")
    except requests.RequestException as e:
        return AuthCheckResult(service, False, f"Network error: {e}")

def pre_flight_auth_check(tokens: dict[str, str]) -> tuple[bool, str]:
    """
    Check all required service tokens before starting a task.
    Returns (all_healthy, error_summary).
    """
    CHECK_URLS = {
        "github": "https://api.github.com/user",
        "slack": "https://slack.com/api/auth.test",
        "google": "https://www.googleapis.com/oauth2/v1/userinfo",
    }

    results = [
        check_token(service, CHECK_URLS[service], tokens[service])
        for service in tokens
        if service in CHECK_URLS
    ]

    failed = [r for r in results if not r.healthy]
    if not failed:
        return True, ""

    error_lines = [f"• {r.service}: {r.error}" for r in failed]
    return False, (
        "The following services need re-authentication before this task can proceed:\n"
        + "\n".join(error_lines)
        + "\n\nPlease reconnect these services and try again."
    )

def run_agent_task(task: str, tokens: dict[str, str]) -> str:
    all_healthy, error_msg = pre_flight_auth_check(tokens)

    if not all_healthy:
        return f"Pre-flight auth check failed:\n{error_msg}"

    client = anthropic.Anthropic()
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        system="You are a helpful assistant with access to GitHub and Slack.",
        messages=[{"role": "user", "content": task}],
    )
    return response.content[0].text

# Test: expired GitHub token caught before task starts
result = run_agent_task(
    task="Summarise my recent GitHub activity and post it to Slack.",
    tokens={
        "github": "expired-gh-token",
        "slack": "valid-sl-token",
    },
)
print(result)
```

**Expected Token Savings:** ~40% — catches auth failures before multi-step tasks consume turns
**Environment:** `pip install anthropic requests`

---

## Comparison

| Option | When It Acts | Auto-Refresh | User Notification | Best For |
|--------|-------------|--------------|-------------------|----------|
| Explicit Auth Error Propagation | On 401 | No | Immediate | All agents (always apply) |
| Auto Token Refresh + Retry | On 401 | Yes | Only if refresh fails | Production OAuth integrations |
| Proactive Expiry Check | Before call | Yes | Silent | High-reliability services |
| Tool Result Auth Envelope | On 401 | No | Via agent | Structured multi-tool agents |
| Centralised Token Manager | Before call | Yes | On failure | Multi-service agents |
| Pre-Flight Health Check | Before task | No | Before task starts | Long multi-step tasks |

**Recommended starting point:** Option 1 (Explicit Auth Error Propagation) for immediate correctness; add Option 2 (Auto Refresh) for any production agent using OAuth-protected APIs.
