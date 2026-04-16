---
title: "Agent Doesn't Implement OAuth2 Device Flow for Headless Agents"
description: "AI agents running in headless environments (servers, containers, CLI tools) cannot redirect users to a browser for OAuth2 authorization. The Device Authorization Grant (RFC 8628) solves this by letting the agent display a code the user enters on any device, then polling for the access token."
date: 2025-02-04
difficulty: intermediate
category: security
slug: agent-doesnt-implement-oauth2-device-flow-for-headless-agents
tags:
  - oauth2
  - device-flow
  - authentication
  - headless
  - authorization
  - rfc8628
  - security
symptoms:
  - "Agent running in a container cannot open a browser for user OAuth authorization"
  - "CLI agent prompts users for credentials directly instead of using OAuth2"
  - "Agent stores long-lived refresh tokens in files with no revocation mechanism"
  - "SSH-connected agents require copy-pasting URLs that don't work in terminal environments"
  - "Service account credentials are shared across multiple users with no per-user audit trail"
---

## Problem

Standard OAuth2 flows (authorization code, implicit) require a browser redirect to the authorization server. This is impossible when the agent runs:

- In a Docker container with no GUI.
- Via SSH on a remote server.
- As a background service or daemon.
- As a CLI tool invoked from a script.

The **Device Authorization Grant** (RFC 8628) was designed exactly for this case:
1. The agent calls the device authorization endpoint and receives a `device_code` and `user_code`.
2. The agent displays a URL + `user_code` to the user (in the terminal or via any side channel).
3. The user opens the URL on any device (phone, laptop) and enters the code.
4. The agent polls the token endpoint until the user approves.
5. The agent receives an access token (and optionally a refresh token).

---

## Solution 1: Device Flow Client (Core RFC 8628 Implementation)

```python
import asyncio
import json
import time
from dataclasses import dataclass
from typing import Optional

try:
    import aiohttp
    HAS_AIOHTTP = True
except ImportError:
    HAS_AIOHTTP = False


@dataclass
class DeviceAuthorizationResponse:
    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: Optional[str]
    expires_in: int
    interval: int      # polling interval in seconds


@dataclass
class TokenResponse:
    access_token: str
    token_type: str
    expires_in: Optional[int]
    refresh_token: Optional[str]
    scope: Optional[str]
    id_token: Optional[str]
    obtained_at: float = 0.0

    def __post_init__(self):
        if not self.obtained_at:
            self.obtained_at = time.time()

    def is_expired(self) -> bool:
        if self.expires_in is None:
            return False
        return time.time() > self.obtained_at + self.expires_in - 30


class DeviceFlowClient:
    """
    RFC 8628 Device Authorization Grant client.

    Usage:
        client = DeviceFlowClient(
            client_id="my-agent",
            device_authorization_endpoint="https://auth.example.com/device",
            token_endpoint="https://auth.example.com/token",
        )
        auth = await client.start_device_flow(scope="openid profile")
        print(f"Visit {auth.verification_uri} and enter code: {auth.user_code}")
        tokens = await client.poll_for_token(auth)
        print(f"Access token: {tokens.access_token[:20]}...")
    """

    def __init__(self, client_id: str,
                 device_authorization_endpoint: str,
                 token_endpoint: str,
                 client_secret: Optional[str] = None):
        self._client_id = client_id
        self._device_ep = device_authorization_endpoint
        self._token_ep = token_endpoint
        self._client_secret = client_secret

    async def start_device_flow(self, scope: str = "openid") -> DeviceAuthorizationResponse:
        if not HAS_AIOHTTP:
            raise ImportError("aiohttp required")
        data = {"client_id": self._client_id, "scope": scope}
        if self._client_secret:
            data["client_secret"] = self._client_secret
        async with aiohttp.ClientSession() as session:
            async with session.post(self._device_ep, data=data) as resp:
                body = await resp.json()
        return DeviceAuthorizationResponse(
            device_code=body["device_code"],
            user_code=body["user_code"],
            verification_uri=body["verification_uri"],
            verification_uri_complete=body.get("verification_uri_complete"),
            expires_in=body.get("expires_in", 1800),
            interval=body.get("interval", 5),
        )

    async def poll_for_token(self, auth: DeviceAuthorizationResponse,
                              timeout: Optional[float] = None) -> TokenResponse:
        deadline = time.time() + (timeout or auth.expires_in)
        interval = auth.interval
        data = {
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": auth.device_code,
            "client_id": self._client_id,
        }
        if self._client_secret:
            data["client_secret"] = self._client_secret

        async with aiohttp.ClientSession() as session:
            while time.time() < deadline:
                await asyncio.sleep(interval)
                async with session.post(self._token_ep, data=data) as resp:
                    body = await resp.json()
                    if resp.status == 200:
                        return TokenResponse(
                            access_token=body["access_token"],
                            token_type=body.get("token_type", "Bearer"),
                            expires_in=body.get("expires_in"),
                            refresh_token=body.get("refresh_token"),
                            scope=body.get("scope"),
                            id_token=body.get("id_token"),
                        )
                    error = body.get("error", "")
                    if error == "authorization_pending":
                        continue
                    elif error == "slow_down":
                        interval += 5
                        continue
                    elif error == "access_denied":
                        raise PermissionError("User denied authorization")
                    elif error == "expired_token":
                        raise TimeoutError("Device code expired")
                    else:
                        raise RuntimeError(f"Token error: {error}: {body.get('error_description')}")
        raise TimeoutError("Device flow timeout")
```

---

## Solution 2: Token Cache with Automatic Refresh

Persist tokens to an encrypted local file. On subsequent runs, load the cached token and refresh it if expired — the user only has to go through the device flow once.

```python
import base64
import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional


class TokenCache:
    """
    Encrypts and persists OAuth2 tokens to disk.
    On next run, loads and refreshes expired tokens automatically.

    Usage:
        cache = TokenCache("~/.agent/tokens.enc", secret_key=b"32-byte-key-here-00000000000000")
        cached = cache.load("my-agent")
        if cached and not cached.is_expired():
            tokens = cached
        else:
            tokens = await device_flow_client.poll_for_token(auth)
            cache.save("my-agent", tokens)
    """

    def __init__(self, path: str, secret_key: bytes):
        self._path = Path(path).expanduser()
        self._key = secret_key

    def _encrypt(self, data: bytes) -> bytes:
        try:
            from cryptography.fernet import Fernet
            import base64, hashlib
            key = base64.urlsafe_b64encode(hashlib.sha256(self._key).digest())
            return Fernet(key).encrypt(data)
        except ImportError:
            return base64.b64encode(data)  # fallback: base64 only (not secure)

    def _decrypt(self, data: bytes) -> bytes:
        try:
            from cryptography.fernet import Fernet
            import base64, hashlib
            key = base64.urlsafe_b64encode(hashlib.sha256(self._key).digest())
            return Fernet(key).decrypt(data)
        except ImportError:
            return base64.b64decode(data)

    def save(self, agent_id: str, tokens: TokenResponse):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        existing = {}
        if self._path.exists():
            try:
                existing = json.loads(self._decrypt(self._path.read_bytes()))
            except Exception:
                pass
        existing[agent_id] = asdict(tokens)
        self._path.write_bytes(self._encrypt(json.dumps(existing).encode()))
        self._path.chmod(0o600)

    def load(self, agent_id: str) -> Optional[TokenResponse]:
        if not self._path.exists():
            return None
        try:
            data = json.loads(self._decrypt(self._path.read_bytes()))
            entry = data.get(agent_id)
            if not entry:
                return None
            return TokenResponse(**entry)
        except Exception:
            return None

    def delete(self, agent_id: str):
        if not self._path.exists():
            return
        try:
            data = json.loads(self._decrypt(self._path.read_bytes()))
            data.pop(agent_id, None)
            self._path.write_bytes(self._encrypt(json.dumps(data).encode()))
        except Exception:
            pass
```

---

## Solution 3: Token Refresh Manager

Automatically refreshes access tokens before they expire using the stored refresh token. Eliminates the need to repeat the device flow.

```python
import asyncio
import time
from typing import Optional

try:
    import aiohttp
    HAS_AIOHTTP = True
except ImportError:
    HAS_AIOHTTP = False


class TokenRefreshManager:
    """
    Keeps a valid access token by refreshing proactively before expiry.

    Usage:
        mgr = TokenRefreshManager(
            token_endpoint="https://auth.example.com/token",
            client_id="my-agent",
            refresh_threshold=300,   # refresh 5 min before expiry
        )
        mgr.set_tokens(initial_tokens)
        asyncio.create_task(mgr.refresh_loop())

        token = await mgr.get_valid_token()
        headers = {"Authorization": f"Bearer {token}"}
    """

    def __init__(self, token_endpoint: str, client_id: str,
                 client_secret: Optional[str] = None,
                 refresh_threshold: int = 300):
        self._token_ep = token_endpoint
        self._client_id = client_id
        self._client_secret = client_secret
        self._threshold = refresh_threshold
        self._tokens: Optional[TokenResponse] = None
        self._lock = asyncio.Lock()

    def set_tokens(self, tokens: TokenResponse):
        self._tokens = tokens

    async def get_valid_token(self) -> str:
        async with self._lock:
            if self._tokens is None:
                raise RuntimeError("No tokens available. Run device flow first.")
            if self._tokens.is_expired():
                await self._refresh()
            return self._tokens.access_token

    async def _refresh(self):
        if not self._tokens or not self._tokens.refresh_token:
            raise RuntimeError("No refresh token available.")
        data = {
            "grant_type": "refresh_token",
            "refresh_token": self._tokens.refresh_token,
            "client_id": self._client_id,
        }
        if self._client_secret:
            data["client_secret"] = self._client_secret

        async with aiohttp.ClientSession() as session:
            async with session.post(self._token_ep, data=data) as resp:
                body = await resp.json()
                if resp.status != 200:
                    raise RuntimeError(f"Token refresh failed: {body}")
                self._tokens = TokenResponse(
                    access_token=body["access_token"],
                    token_type=body.get("token_type", "Bearer"),
                    expires_in=body.get("expires_in"),
                    refresh_token=body.get("refresh_token", self._tokens.refresh_token),
                    scope=body.get("scope"),
                    id_token=body.get("id_token"),
                )

    async def refresh_loop(self):
        while True:
            await asyncio.sleep(60)
            if self._tokens and not self._tokens.is_expired():
                ttl = (self._tokens.obtained_at + (self._tokens.expires_in or 3600)) - time.time()
                if ttl < self._threshold:
                    async with self._lock:
                        await self._refresh()
```

---

## Solution 4: Multi-Tenant Device Flow Orchestrator

Manages device flows for multiple users simultaneously. Each user gets their own flow; the orchestrator tracks state and dispatches tokens when authorization completes.

```python
import asyncio
import time
from dataclasses import dataclass
from typing import Callable, Dict, Optional


@dataclass
class UserFlowState:
    user_id: str
    auth: DeviceAuthorizationResponse
    started_at: float
    status: str = "pending"   # pending | completed | expired | denied
    tokens: Optional[TokenResponse] = None


class MultiTenantDeviceFlowOrchestrator:
    """
    Manages concurrent device flows for multiple users.

    Usage:
        orch = MultiTenantDeviceFlowOrchestrator(device_client)
        orch.on_authorized(lambda uid, tokens: save_tokens(uid, tokens))

        flow_state = await orch.start_flow("user-123", scope="openid profile")
        # Display flow_state.auth.verification_uri to the user
        # Background poller completes automatically
    """

    def __init__(self, device_client: DeviceFlowClient,
                 max_concurrent: int = 50):
        self._client = device_client
        self._flows: Dict[str, UserFlowState] = {}
        self._max = max_concurrent
        self._callbacks: list = []

    def on_authorized(self, callback: Callable[[str, TokenResponse], None]):
        self._callbacks.append(callback)

    async def start_flow(self, user_id: str,
                          scope: str = "openid") -> UserFlowState:
        if len(self._flows) >= self._max:
            raise RuntimeError("Max concurrent device flows reached")
        auth = await self._client.start_device_flow(scope)
        state = UserFlowState(
            user_id=user_id, auth=auth, started_at=time.time()
        )
        self._flows[user_id] = state
        asyncio.create_task(self._poll_user(user_id, state))
        return state

    async def _poll_user(self, user_id: str, state: UserFlowState):
        try:
            tokens = await self._client.poll_for_token(state.auth)
            state.tokens = tokens
            state.status = "completed"
            for cb in self._callbacks:
                cb(user_id, tokens)
        except PermissionError:
            state.status = "denied"
        except TimeoutError:
            state.status = "expired"
        except Exception:
            state.status = "error"
        finally:
            self._flows.pop(user_id, None)

    def status(self, user_id: str) -> Optional[str]:
        state = self._flows.get(user_id)
        return state.status if state else None

    def pending_count(self) -> int:
        return len(self._flows)
```

---

## Solution 5: CLI Display Helper

Renders the device code instruction in the terminal with ASCII art, QR code (if available), and a countdown timer.

```python
import sys
import time


class DeviceFlowCLIPresenter:
    """
    Renders device flow instructions in a terminal.

    Usage:
        presenter = DeviceFlowCLIPresenter()
        presenter.show(auth_response)
        # Displays: URL, user code, expiry countdown
    """

    def show(self, auth: DeviceAuthorizationResponse,
              label: str = "Agent Authorization Required"):
        width = 60
        print("\n" + "=" * width)
        print(f"  {label}")
        print("=" * width)
        print()
        print(f"  1. Open this URL on any device:")
        print(f"     {auth.verification_uri}")
        print()
        print(f"  2. Enter this code when prompted:")
        print()
        code = auth.user_code
        # Format code with spaces for readability (e.g. ABCD-EFGH)
        if len(code) == 8 and "-" not in code:
            code = f"{code[:4]}-{code[4:]}"
        print(f"     >>> {code} <<<")
        print()
        if auth.verification_uri_complete:
            print(f"  (Or visit the complete URL to skip code entry)")
        expires_min = auth.expires_in // 60
        print(f"  Code expires in {expires_min} minutes.")
        print()
        self._try_qr(auth.verification_uri_complete or auth.verification_uri)
        print("=" * width + "\n")

    def _try_qr(self, url: str):
        try:
            import qrcode
            qr = qrcode.QRCode(border=1)
            qr.add_data(url)
            qr.make()
            qr.print_ascii(invert=True)
            print()
        except ImportError:
            pass  # qrcode not installed; skip

    def wait_with_spinner(self, auth: DeviceAuthorizationResponse):
        """Show a spinner while polling."""
        frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
        deadline = time.time() + auth.expires_in
        i = 0
        while time.time() < deadline:
            remaining = int(deadline - time.time())
            sys.stdout.write(
                f"\r  {frames[i % len(frames)]} Waiting for authorization... "
                f"({remaining}s remaining)  "
            )
            sys.stdout.flush()
            time.sleep(0.2)
            i += 1
        print("\r  Timed out waiting for authorization.          ")
```

---

## Solution 6: Unified Headless OAuth2 Agent

Combines device flow, token caching, refresh, and CLI display into a single facade for headless agent authentication.

```python
import asyncio
import os
from pathlib import Path
from typing import Optional


class HeadlessOAuth2Agent:
    """
    Complete headless OAuth2 solution using the Device Authorization Grant.

    Usage:
        agent = HeadlessOAuth2Agent(
            client_id="my-agent",
            device_authorization_endpoint="https://auth.example.com/device",
            token_endpoint="https://auth.example.com/token",
            cache_path="~/.agent/tokens.enc",
            cache_secret=os.environ["TOKEN_CACHE_SECRET"].encode(),
        )
        await agent.authenticate(user_id="user-123", scope="openid profile")
        token = await agent.get_token("user-123")
        headers = {"Authorization": f"Bearer {token}"}
    """

    def __init__(self, client_id: str,
                 device_authorization_endpoint: str,
                 token_endpoint: str,
                 cache_path: str = "~/.agent/tokens.enc",
                 cache_secret: Optional[bytes] = None,
                 client_secret: Optional[str] = None):
        self._client = DeviceFlowClient(
            client_id=client_id,
            device_authorization_endpoint=device_authorization_endpoint,
            token_endpoint=token_endpoint,
            client_secret=client_secret,
        )
        secret = cache_secret or os.urandom(32)
        self._cache = TokenCache(cache_path, secret_key=secret)
        self._refresh_managers: dict = {}
        self._presenter = DeviceFlowCLIPresenter()
        self._token_ep = token_endpoint
        self._client_id = client_id
        self._client_secret = client_secret

    async def authenticate(self, user_id: str = "default",
                            scope: str = "openid") -> TokenResponse:
        # Try cached token first
        cached = self._cache.load(user_id)
        if cached and cached.refresh_token:
            mgr = TokenRefreshManager(
                self._token_ep, self._client_id, self._client_secret
            )
            mgr.set_tokens(cached)
            try:
                token_str = await mgr.get_valid_token()
                self._refresh_managers[user_id] = mgr
                return cached
            except Exception:
                pass  # Fall through to device flow

        # Run device flow
        auth = await self._client.start_device_flow(scope)
        self._presenter.show(auth)

        tokens = await self._client.poll_for_token(auth)
        self._cache.save(user_id, tokens)

        mgr = TokenRefreshManager(self._token_ep, self._client_id, self._client_secret)
        mgr.set_tokens(tokens)
        asyncio.create_task(mgr.refresh_loop())
        self._refresh_managers[user_id] = mgr
        return tokens

    async def get_token(self, user_id: str = "default") -> str:
        mgr = self._refresh_managers.get(user_id)
        if mgr is None:
            raise RuntimeError(f"User '{user_id}' not authenticated. Call authenticate() first.")
        return await mgr.get_valid_token()

    def logout(self, user_id: str = "default"):
        self._cache.delete(user_id)
        self._refresh_managers.pop(user_id, None)
```

---

## Comparison

| Approach | User Friction | Requires Browser | Token Persistence |
|---|---|---|---|
| **Device Flow Client** | Low (code entry) | No | No |
| **Token Cache** | Zero (after first auth) | No | Encrypted file |
| **Token Refresh Manager** | Zero (background) | No | In memory |
| **Multi-Tenant Orchestrator** | Low | No | Caller-managed |
| **CLI Display Helper** | Low (QR option) | No | No |
| **Unified Headless Agent** | Zero (cached) | No | Encrypted file |

**Key insight**: always store the refresh token, not just the access token. With refresh token rotation enabled at the authorization server, you get long-lived sessions with automatic security invalidation when a token is compromised.
