---
layout: solution
title: "Agent Doesn't Implement OAuth2 Token Refresh for Long-Running Tasks"
category: auth
description: "Agent starts a long multi-step task with a valid OAuth2 access token but doesn't refresh it — causing 401 errors mid-task when the token expires after 30-60 minutes, losing all progress."
tags: [auth, oauth2, token-refresh, long-running-tasks, resilience]
---

# Agent Doesn't Implement OAuth2 Token Refresh for Long-Running Tasks

## Problem

OAuth2 access tokens typically expire after 30–60 minutes. An agent that starts a long batch task (document processing, multi-step analysis, data migration) with a valid token will hit a 401 Unauthorized error mid-task when the token expires — with no recovery mechanism.

**Root cause:** The agent stores the access token at startup but never checks expiry or refreshes it during execution. Tool calls that require authentication fail silently or crash the task.

**Symptoms:**
- "401 Unauthorized" errors that appear only after the agent has been running for ~1 hour
- Tasks that work in testing (short duration) but fail in production (long duration)
- No automatic recovery — the entire task must restart from scratch
- Lost progress when token expires at step 8 of a 10-step task

---

## Option 1: Token Wrapper with Automatic Refresh Before Each Call

Wrap every authenticated tool call with a token check; refresh proactively when expiry is near.

```python
import anthropic
import json
import time
from dataclasses import dataclass, field

client = anthropic.Anthropic()

@dataclass
class OAuthToken:
    access_token: str
    refresh_token: str
    expires_at: float  # Unix timestamp
    token_type: str = "Bearer"
    scope: str = ""

    @property
    def is_expired(self) -> bool:
        return time.time() >= self.expires_at

    @property
    def expires_in_seconds(self) -> float:
        return self.expires_at - time.time()

    @property
    def needs_refresh(self) -> bool:
        """Refresh if less than 5 minutes remaining."""
        return self.expires_in_seconds < 300

class TokenManager:
    def __init__(self, token: OAuthToken, refresh_url: str, client_id: str, client_secret: str):
        self.token = token
        self.refresh_url = refresh_url
        self.client_id = client_id
        self.client_secret = client_secret
        self._refresh_count = 0

    def get_valid_token(self) -> OAuthToken:
        """Return a valid token, refreshing if necessary."""
        if self.token.needs_refresh or self.token.is_expired:
            self._refresh()
        return self.token

    def _refresh(self):
        """Call the OAuth2 token endpoint to get a new access token."""
        print(f"[oauth] Refreshing token (refresh #{self._refresh_count + 1}, "
              f"was expiring in {self.token.expires_in_seconds:.0f}s)")

        # In production: real HTTP call to self.refresh_url
        # import httpx
        # resp = httpx.post(self.refresh_url, data={
        #     "grant_type": "refresh_token",
        #     "refresh_token": self.token.refresh_token,
        #     "client_id": self.client_id,
        #     "client_secret": self.client_secret,
        # })
        # data = resp.json()
        # Simulated response:
        data = {
            "access_token": f"new_access_token_{int(time.time())}",
            "refresh_token": self.token.refresh_token,  # Often unchanged
            "expires_in": 3600,  # 1 hour
            "token_type": "Bearer"
        }

        self.token = OAuthToken(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token", self.token.refresh_token),
            expires_at=time.time() + data["expires_in"],
            token_type=data["token_type"]
        )
        self._refresh_count += 1
        print(f"[oauth] Token refreshed. New expiry in {data['expires_in']}s")

    def authenticated_header(self) -> dict[str, str]:
        token = self.get_valid_token()
        return {"Authorization": f"{token.token_type} {token.access_token}"}

# Initialize with a short-lived token (simulated: expires in 10 seconds for demo)
token_manager = TokenManager(
    token=OAuthToken(
        access_token="initial_access_token",
        refresh_token="refresh_token_abc123",
        expires_at=time.time() + 10  # Expires in 10s (demo)
    ),
    refresh_url="https://auth.example.com/oauth/token",
    client_id="agent_client_id",
    client_secret="secret"
)

def authenticated_api_call(endpoint: str, data: dict) -> dict:
    """Make an API call using a proactively refreshed token."""
    headers = token_manager.authenticated_header()
    # In production: real HTTP call
    return {
        "endpoint": endpoint,
        "status": "ok",
        "token_used": token_manager.token.access_token[-8:],
        "data": data
    }

tools = [
    {
        "name": "process_document",
        "description": "Process a document via authenticated API",
        "input_schema": {
            "type": "object",
            "properties": {
                "doc_id": {"type": "string"},
                "operation": {"type": "string"}
            },
            "required": ["doc_id", "operation"]
        }
    }
]

def run_long_task_with_token_refresh(query: str) -> str:
    messages = [{"role": "user", "content": query}]

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=tools,
            messages=messages
        )

        if response.stop_reason == "end_turn":
            return next(b.text for b in response.content if hasattr(b, "text"))

        if response.stop_reason != "tool_use":
            break

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            # Token is checked and refreshed transparently before every call
            result = authenticated_api_call(
                endpoint=f"/api/documents/{block.input['doc_id']}",
                data=block.input
            )
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(result)
            })
            time.sleep(0.1)  # Simulate processing time

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

    return "Done"

result = run_long_task_with_token_refresh(
    "Process documents DOC-001, DOC-002, and DOC-003 with operation 'analyze'"
)
print(result)
print(f"[oauth] Total refreshes: {token_manager._refresh_count}")

# Expected Token Savings: ~0% (auth is orthogonal to token cost; prevents task failure and costly restarts)
# Environment: Any agent calling OAuth2-protected APIs (Google, Microsoft, Salesforce, GitHub)
```

---

## Option 2: Token Pool with Rotation for High-Throughput Agents

Maintain a pool of tokens across multiple OAuth clients; rotate when one expires.

```python
import anthropic
import json
import time
from dataclasses import dataclass, field
from collections import deque

client = anthropic.Anthropic()

@dataclass
class PooledToken:
    client_id: str
    access_token: str
    refresh_token: str
    expires_at: float
    use_count: int = 0

    @property
    def is_usable(self) -> bool:
        return time.time() < self.expires_at - 60  # 1 min buffer

class TokenPool:
    """Round-robin pool of OAuth tokens for high-throughput use."""
    def __init__(self, tokens: list[PooledToken]):
        self.pool: deque[PooledToken] = deque(tokens)
        self.refresh_log: list[dict] = []

    def get_token(self) -> PooledToken:
        """Get the next usable token, refreshing stale ones."""
        for _ in range(len(self.pool)):
            token = self.pool[0]
            self.pool.rotate(-1)

            if token.is_usable:
                token.use_count += 1
                return token

            # Refresh this token
            self._refresh_token(token)
            if token.is_usable:
                token.use_count += 1
                return token

        raise RuntimeError("No usable tokens in pool")

    def _refresh_token(self, token: PooledToken):
        print(f"[pool] Refreshing token for client {token.client_id}")
        # Simulate refresh
        token.access_token = f"refreshed_{token.client_id}_{int(time.time())}"
        token.expires_at = time.time() + 3600
        self.refresh_log.append({"client": token.client_id, "at": time.time()})

    @property
    def status(self) -> list[dict]:
        return [
            {
                "client": t.client_id,
                "usable": t.is_usable,
                "expires_in": round(t.expires_at - time.time()),
                "uses": t.use_count
            }
            for t in self.pool
        ]

pool = TokenPool([
    PooledToken("client-A", "token-A-001", "refresh-A", time.time() + 3600),
    PooledToken("client-B", "token-B-001", "refresh-B", time.time() + 1800),
    PooledToken("client-C", "token-C-001", "refresh-C", time.time() + 7200),
])

def pooled_api_call(resource: str) -> dict:
    token = pool.get_token()
    return {
        "resource": resource,
        "client_used": token.client_id,
        "token_suffix": token.access_token[-6:],
        "status": "ok"
    }

tools = [
    {
        "name": "fetch_resource",
        "description": "Fetch a protected resource",
        "input_schema": {
            "type": "object",
            "properties": {"resource": {"type": "string"}},
            "required": ["resource"]
        }
    }
]

def run_pooled_token_agent(query: str) -> str:
    messages = [{"role": "user", "content": query}]
    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=tools,
            messages=messages
        )
        if response.stop_reason == "end_turn":
            print(f"[pool] Final pool status: {pool.status}")
            return next(b.text for b in response.content if hasattr(b, "text"))
        if response.stop_reason != "tool_use":
            break
        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            result = pooled_api_call(block.input["resource"])
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(result)
            })
        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})
    return "Done"

print(run_pooled_token_agent("Fetch resources: /users/profile, /data/reports, /config/settings"))

# Expected Token Savings: ~0% (pool prevents auth failures that would require task restart)
# Environment: High-throughput agents making many concurrent authenticated calls; multi-tenant SaaS agents
```

---

## Option 3: Retry-on-401 with Automatic Token Refresh

Catch 401 errors from tool calls; refresh the token and retry the failed call transparently.

```python
import anthropic
import json
import time

client = anthropic.Anthropic()

class TokenRefreshOnError:
    def __init__(self):
        self.access_token = "valid_initial_token"
        self.refresh_token = "refresh_abc"
        self._call_count = 0
        self._refresh_count = 0

    def refresh(self) -> bool:
        """Refresh the access token. Returns True on success."""
        print(f"[retry-401] Refreshing token after 401 error")
        # Simulate refresh call
        self.access_token = f"refreshed_token_{int(time.time())}"
        self._refresh_count += 1
        print(f"[retry-401] Token refreshed (refresh #{self._refresh_count})")
        return True

    def call(self, endpoint: str, payload: dict) -> tuple[int, dict]:
        """Simulate API call that may return 401 after some calls."""
        self._call_count += 1
        # Simulate token expiry after 2 calls
        if self._call_count == 3 and not self.access_token.startswith("refreshed"):
            return 401, {"error": "token_expired", "message": "Access token has expired"}
        return 200, {"endpoint": endpoint, "result": "ok", "call_num": self._call_count}

    def call_with_retry(self, endpoint: str, payload: dict, max_retries: int = 2) -> dict:
        """Call with automatic token refresh on 401."""
        for attempt in range(1, max_retries + 1):
            status, result = self.call(endpoint, payload)

            if status == 200:
                return result

            if status == 401 and attempt < max_retries:
                print(f"[retry-401] Got 401 on attempt {attempt}, refreshing token...")
                if self.refresh():
                    continue  # Retry with new token
                else:
                    return {"error": "Token refresh failed — cannot authenticate"}

            return {"error": f"HTTP {status}", "details": result}

        return {"error": "Max retries exceeded"}

auth_client = TokenRefreshOnError()

tools = [
    {
        "name": "api_call",
        "description": "Call a protected API endpoint",
        "input_schema": {
            "type": "object",
            "properties": {
                "endpoint": {"type": "string"},
                "action": {"type": "string"}
            },
            "required": ["endpoint"]
        }
    }
]

def run_retry_on_401_agent(query: str) -> str:
    messages = [{"role": "user", "content": query}]
    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=tools,
            messages=messages
        )
        if response.stop_reason == "end_turn":
            return next(b.text for b in response.content if hasattr(b, "text"))
        if response.stop_reason != "tool_use":
            break
        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            result = auth_client.call_with_retry(
                block.input["endpoint"],
                block.input
            )
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(result)
            })
        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})
    return "Done"

result = run_retry_on_401_agent(
    "Call these endpoints: /users/list, /reports/summary, /data/export, /config/get"
)
print(result)

# Expected Token Savings: ~15% (transparent retry avoids a full agent turn to report 401 and re-plan)
# Environment: Agents using APIs with short-lived tokens (Google APIs, Microsoft Graph, Salesforce)
```

---

## Option 4: Async Token Refresh with Lock — Thread-Safe for Concurrent Tool Calls

Async-safe token refresh using a lock to prevent multiple concurrent refresh calls.

```python
import anthropic
import asyncio
import json
import time
from dataclasses import dataclass

client = anthropic.AsyncAnthropic()

@dataclass
class AsyncOAuthToken:
    access_token: str
    refresh_token: str
    expires_at: float

    @property
    def is_expired(self) -> bool:
        return time.time() >= self.expires_at - 60  # 60s buffer

class AsyncTokenManager:
    def __init__(self, initial_token: AsyncOAuthToken):
        self._token = initial_token
        self._lock = asyncio.Lock()
        self._refresh_in_progress = False

    async def get_token(self) -> str:
        if not self._token.is_expired:
            return self._token.access_token

        async with self._lock:
            # Double-check after acquiring lock (another coroutine may have refreshed)
            if not self._token.is_expired:
                return self._token.access_token

            await self._refresh()
            return self._token.access_token

    async def _refresh(self):
        print(f"[async-oauth] Refreshing token...")
        await asyncio.sleep(0.1)  # Simulate async HTTP call to token endpoint
        self._token = AsyncOAuthToken(
            access_token=f"async_token_{int(time.time())}",
            refresh_token=self._token.refresh_token,
            expires_at=time.time() + 3600
        )
        print(f"[async-oauth] Token refreshed: ...{self._token.access_token[-8:]}")

async_token_mgr = AsyncTokenManager(
    AsyncOAuthToken(
        access_token="initial_async_token",
        refresh_token="async_refresh_xyz",
        expires_at=time.time() + 5  # Expires in 5s for demo
    )
)

async def async_api_call(endpoint: str, payload: dict) -> dict:
    token = await async_token_mgr.get_token()
    await asyncio.sleep(0.05)  # Simulate API latency
    return {"endpoint": endpoint, "token": token[-8:], "status": "ok"}

tools = [
    {
        "name": "async_fetch",
        "description": "Fetch data from an authenticated endpoint",
        "input_schema": {
            "type": "object",
            "properties": {
                "endpoint": {"type": "string"},
                "key": {"type": "string"}
            },
            "required": ["endpoint"]
        }
    }
]

async def run_async_token_agent(query: str) -> str:
    messages = [{"role": "user", "content": query}]
    while True:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=tools,
            messages=messages
        )
        if response.stop_reason == "end_turn":
            return next(b.text for b in response.content if hasattr(b, "text"))
        if response.stop_reason != "tool_use":
            break

        tool_blocks = [b for b in response.content if b.type == "tool_use"]

        # Execute all tool calls concurrently — all share the same token manager
        async def exec_block(block) -> dict:
            result = await async_api_call(block.input["endpoint"], block.input)
            return {"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result)}

        tool_results = await asyncio.gather(*[exec_block(b) for b in tool_blocks])

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": list(tool_results)})

    return "Done"

result = asyncio.run(run_async_token_agent(
    "Fetch data from /api/users, /api/orders, and /api/inventory simultaneously"
))
print(result)

# Expected Token Savings: ~0% (lock prevents thundering herd refresh; all concurrent calls share one refresh)
# Environment: High-concurrency async agents; serverless functions with shared token state
```

---

## Option 5: Token Refresh Scheduler — Background Proactive Refresh

Schedule token refresh in the background before expiry, so it's always ready.

```python
import anthropic
import asyncio
import json
import time
from dataclasses import dataclass

client = anthropic.AsyncAnthropic()

@dataclass
class ManagedToken:
    access_token: str
    refresh_token: str
    expires_at: float
    refresh_margin_s: float = 300  # Refresh 5 min before expiry

    @property
    def refresh_at(self) -> float:
        return self.expires_at - self.refresh_margin_s

    @property
    def seconds_until_refresh(self) -> float:
        return max(0, self.refresh_at - time.time())

class BackgroundTokenRefresher:
    def __init__(self, token: ManagedToken):
        self.token = token
        self._task: asyncio.Task | None = None
        self._refresh_count = 0

    async def start(self):
        """Start background refresh scheduler."""
        self._task = asyncio.create_task(self._refresh_loop())
        print(f"[bg-refresh] Scheduler started. Next refresh in {self.token.seconds_until_refresh:.0f}s")

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _refresh_loop(self):
        while True:
            wait_time = self.token.seconds_until_refresh
            if wait_time > 0:
                await asyncio.sleep(wait_time)
            await self._do_refresh()

    async def _do_refresh(self):
        print(f"[bg-refresh] Proactively refreshing token...")
        await asyncio.sleep(0.1)  # Simulate HTTP call
        self.token.access_token = f"bg_refreshed_{int(time.time())}"
        self.token.expires_at = time.time() + 3600
        self._refresh_count += 1
        print(f"[bg-refresh] Done. Next refresh in {self.token.seconds_until_refresh:.0f}s (refresh #{self._refresh_count})")

    def get_token(self) -> str:
        """Always returns the current (possibly just-refreshed) token."""
        return self.token.access_token

async def run_with_background_refresh(query: str) -> str:
    token = ManagedToken(
        access_token="initial_token",
        refresh_token="refresh_xyz",
        expires_at=time.time() + 3600,
        refresh_margin_s=3595  # Demo: refresh almost immediately
    )
    refresher = BackgroundTokenRefresher(token)
    await refresher.start()

    tools = [
        {
            "name": "api_action",
            "description": "Perform an authenticated action",
            "input_schema": {
                "type": "object",
                "properties": {"action": {"type": "string"}},
                "required": ["action"]
            }
        }
    ]

    messages = [{"role": "user", "content": query}]
    try:
        while True:
            response = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=512,
                tools=tools,
                messages=messages
            )
            if response.stop_reason == "end_turn":
                return next(b.text for b in response.content if hasattr(b, "text"))
            if response.stop_reason != "tool_use":
                break

            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                current_token = refresher.get_token()
                result = {"action": block.input["action"], "token": current_token[-8:], "ok": True}
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result)
                })

            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})
    finally:
        await refresher.stop()
        print(f"[bg-refresh] Total refreshes: {refresher._refresh_count}")

    return "Done"

result = asyncio.run(run_with_background_refresh(
    "Perform actions: summarize_data, export_report, send_notification"
))
print(result)

# Expected Token Savings: ~0% (background refresh eliminates zero-downtime token gaps)
# Environment: Always-on agents; webhook processors; long-running daemon agents
```

---

## Option 6: Multi-Scope Token Manager — Different Tokens per Tool Category

Maintain separate tokens for different permission scopes; refresh only the relevant scope.

```python
import anthropic
import json
import time
from dataclasses import dataclass, field
from enum import Enum

client = anthropic.Anthropic()

class TokenScope(Enum):
    READ = "read"
    WRITE = "write"
    ADMIN = "admin"
    ANALYTICS = "analytics"

@dataclass
class ScopedToken:
    scope: TokenScope
    access_token: str
    expires_at: float
    refresh_token: str

    @property
    def is_valid(self) -> bool:
        return time.time() < self.expires_at - 30

TOOL_SCOPE_MAP = {
    "read_document": TokenScope.READ,
    "search_data": TokenScope.READ,
    "write_document": TokenScope.WRITE,
    "delete_record": TokenScope.ADMIN,
    "get_analytics": TokenScope.ANALYTICS,
    "update_config": TokenScope.ADMIN,
}

class ScopedTokenManager:
    def __init__(self):
        self._tokens: dict[TokenScope, ScopedToken] = {}
        self._refresh_log: list[dict] = []

    def register(self, scope: TokenScope, access: str, refresh: str, expires_in: float):
        self._tokens[scope] = ScopedToken(
            scope=scope,
            access_token=access,
            expires_at=time.time() + expires_in,
            refresh_token=refresh
        )

    def get_token_for_tool(self, tool_name: str) -> str | None:
        scope = TOOL_SCOPE_MAP.get(tool_name)
        if scope is None:
            return None

        token = self._tokens.get(scope)
        if token is None:
            raise PermissionError(f"No token registered for scope {scope.value}")

        if not token.is_valid:
            self._refresh_scope(scope)

        return self._tokens[scope].access_token

    def _refresh_scope(self, scope: TokenScope):
        old = self._tokens[scope]
        print(f"[scoped] Refreshing {scope.value} scope token")
        # Simulate scope-specific token refresh
        new_token = f"{scope.value}_refreshed_{int(time.time())}"
        self._tokens[scope] = ScopedToken(
            scope=scope,
            access_token=new_token,
            expires_at=time.time() + 3600,
            refresh_token=old.refresh_token
        )
        self._refresh_log.append({"scope": scope.value, "at": time.time()})

mgr = ScopedTokenManager()
mgr.register(TokenScope.READ, "read_token_001", "read_refresh", expires_in=3600)
mgr.register(TokenScope.WRITE, "write_token_001", "write_refresh", expires_in=1800)
mgr.register(TokenScope.ADMIN, "admin_token_001", "admin_refresh", expires_in=900)
mgr.register(TokenScope.ANALYTICS, "analytics_token_001", "analytics_refresh", expires_in=7200)

def execute_scoped_tool(tool_name: str, tool_input: dict) -> dict:
    try:
        token = mgr.get_token_for_tool(tool_name)
    except PermissionError as e:
        return {"error": str(e), "tool": tool_name}

    if token is None:
        return {"error": f"No scope mapping for tool: {tool_name}"}

    scope = TOOL_SCOPE_MAP.get(tool_name)
    return {
        "tool": tool_name,
        "scope": scope.value if scope else "unknown",
        "token_suffix": token[-8:],
        "input": tool_input,
        "status": "ok"
    }

tools = [
    {
        "name": name,
        "description": f"Tool requiring {scope.value} scope",
        "input_schema": {
            "type": "object",
            "properties": {"target": {"type": "string"}},
            "required": ["target"]
        }
    }
    for name, scope in TOOL_SCOPE_MAP.items()
]

def run_scoped_token_agent(query: str) -> str:
    messages = [{"role": "user", "content": query}]
    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=tools,
            messages=messages
        )
        if response.stop_reason == "end_turn":
            print(f"[scoped] Refresh log: {mgr._refresh_log}")
            return next(b.text for b in response.content if hasattr(b, "text"))
        if response.stop_reason != "tool_use":
            break
        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            result = execute_scoped_tool(block.name, block.input)
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(result)
            })
        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})
    return "Done"

result = run_scoped_token_agent(
    "Read document 'report.pdf', then write a summary, then get analytics for the document."
)
print(result)

# Expected Token Savings: ~0% (scope isolation prevents permission escalation and unnecessary token sharing)
# Environment: Enterprise agents with fine-grained OAuth scopes (Google Workspace, Microsoft 365, Salesforce)
```

---

## Comparison

| Option | Refresh Trigger | Thread Safety | Proactive | Scope Isolation | Best For |
|--------|----------------|---------------|-----------|-----------------|----------|
| 1. Pre-Call Check | Before each call | Single-thread | Yes (5min buffer) | No | Simple single-threaded agents |
| 2. Token Pool | On pool selection | Pool-level | No | No | High-throughput multi-client agents |
| 3. Retry-on-401 | On HTTP 401 error | Single-thread | No | No | APIs with reliable 401 responses |
| 4. Async Lock | On expiry check | Yes (asyncio) | No | No | Concurrent async tool execution |
| 5. Background Scheduler | Time-based | Yes (async task) | Yes | No | Always-on daemon agents |
| 6. Scoped Tokens | Per scope, on expiry | Single-thread | Yes | Yes | Enterprise APIs with fine-grained scopes |
