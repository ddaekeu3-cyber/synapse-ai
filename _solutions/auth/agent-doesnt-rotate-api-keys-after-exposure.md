---
layout: solution
title: "Agent Doesn't Rotate API Keys After Exposure — Leaked Key Stays Active"
category: auth
description: "API key appears in a log file, a git commit, or an error message. The team spots it. But the agent is running 24/7 and rotating the key requires downtime. So the exposed key stays active for days while the fix is scheduled. The window between exposure and rotation is when attacks happen."
tags: [auth, api-key, rotation, secrets, security, exposure, zero-downtime, vault, environment, credential-management]
---

# Agent Doesn't Rotate API Keys After Exposure — Leaked Key Stays Active

## Symptom
- API key found in git history — rotation requires redeploying the agent (takes hours to schedule)
- Key appears in error logs that are exported to a third-party log service
- Agent crashes with a stack trace that includes the API key — crash dumps are stored
- Secret scanner alerts fire — but the key can't be rotated without breaking the running agent
- Old key remains active for days because the team doesn't know how to rotate without downtime
- Agent uses a single global API key — rotating it affects all concurrent sessions

## Root Cause
Agents that load API keys once at startup (via env vars or config files) cannot reload them without a restart. When a key is exposed, there's a gap between detection and rotation because rotation requires a deploy cycle. The fix is to design for zero-downtime rotation from the start: load keys from a secrets manager that supports hot-reloading, use short-lived tokens that auto-expire, and build an emergency rotation path that doesn't require a full redeploy.

## Fix

### Option 1: Load keys from secrets manager with hot-reload support

```python
import os
import time
import threading
import logging
from typing import Optional, Callable

logger = logging.getLogger(__name__)

class RotatableSecret:
    """
    Wraps an API key with hot-reload support.
    Polls the secrets manager for updates — rotated keys are picked up without restart.
    """

    def __init__(
        self,
        secret_name: str,
        fetch_fn: Callable[[], str],
        refresh_interval_seconds: int = 300,  # Check for rotation every 5 minutes
        on_rotation: Callable[[str], None] = None
    ):
        self.secret_name = secret_name
        self._fetch_fn = fetch_fn
        self._refresh_interval = refresh_interval_seconds
        self._on_rotation = on_rotation
        self._value: str = ""
        self._last_value: str = ""
        self._lock = threading.RLock()
        self._stop_event = threading.Event()

        # Initial load
        self._refresh()

        # Background refresh thread
        self._thread = threading.Thread(target=self._refresh_loop, daemon=True)
        self._thread.start()

    def _refresh(self):
        """Fetch current secret value from source"""
        try:
            new_value = self._fetch_fn()
            with self._lock:
                if new_value != self._value and self._value:
                    logger.info(f"Secret '{self.secret_name}' rotated — switching to new value")
                    self._last_value = self._value
                    if self._on_rotation:
                        self._on_rotation(new_value)
                self._value = new_value
        except Exception as e:
            logger.error(f"Failed to refresh secret '{self.secret_name}': {e}")

    def _refresh_loop(self):
        while not self._stop_event.wait(timeout=self._refresh_interval):
            self._refresh()

    @property
    def value(self) -> str:
        """Get the current (possibly rotated) secret value"""
        with self._lock:
            return self._value

    def stop(self):
        self._stop_event.set()

def fetch_from_aws_secrets_manager(secret_name: str) -> Callable[[], str]:
    """Returns a fetch function for AWS Secrets Manager"""
    def fetch() -> str:
        import boto3
        client = boto3.client("secretsmanager")
        response = client.get_secret_value(SecretId=secret_name)
        return response["SecretString"]
    return fetch

def fetch_from_env_with_file_watch(env_var: str, file_path: str = None) -> Callable[[], str]:
    """Returns a fetch function that reads from a file (for Kubernetes secrets)"""
    def fetch() -> str:
        # Kubernetes secrets are mounted as files — re-read on each call
        if file_path and os.path.exists(file_path):
            return open(file_path).read().strip()
        return os.environ.get(env_var, "")
    return fetch

# Setup:
anthropic_key = RotatableSecret(
    secret_name="ANTHROPIC_API_KEY",
    fetch_fn=fetch_from_aws_secrets_manager("prod/anthropic/api-key"),
    refresh_interval_seconds=60,  # Check every minute during incident
    on_rotation=lambda new_key: logger.info("Anthropic API key rotated — all new requests will use new key")
)

# Usage — always gets current key:
import anthropic as anthropic_sdk
def get_client() -> anthropic_sdk.Anthropic:
    """Get Anthropic client with current API key — picks up rotations automatically"""
    return anthropic_sdk.Anthropic(api_key=anthropic_key.value)
```

### Option 2: Emergency rotation endpoint — rotate without redeploy

```python
from aiohttp import web
import asyncio
import os
import hashlib
import time
import logging

logger = logging.getLogger(__name__)

class EmergencyRotationController:
    """
    HTTP endpoint that accepts new API keys without restarting the agent.
    Protected by an admin token — the admin token is the one thing that doesn't rotate.
    """

    def __init__(self, admin_token: str):
        self._admin_token = admin_token
        self._secrets: dict[str, str] = {}
        self._rotation_log: list[dict] = []
        self._app = web.Application()
        self._app.router.add_post("/rotate-secret", self._handle_rotation)
        self._app.router.add_get("/secret-status", self._handle_status)

    def set_secret(self, name: str, value: str):
        """Set initial secret value"""
        self._secrets[name] = value

    def get_secret(self, name: str) -> str:
        """Get current secret value — picks up rotations"""
        return self._secrets.get(name, "")

    def _verify_admin(self, request: web.Request) -> bool:
        token = request.headers.get("X-Admin-Token", "")
        return token == self._admin_token

    async def _handle_rotation(self, request: web.Request) -> web.Response:
        """Receive a rotated secret"""
        if not self._verify_admin(request):
            return web.json_response({"error": "Unauthorized"}, status=401)

        try:
            body = await request.json()
            name = body["secret_name"]
            new_value = body["new_value"]

            old_hash = hashlib.sha256(self._secrets.get(name, "").encode()).hexdigest()[:8]
            new_hash = hashlib.sha256(new_value.encode()).hexdigest()[:8]

            self._secrets[name] = new_value
            self._rotation_log.append({
                "secret": name,
                "rotated_at": time.time(),
                "old_hash": old_hash,
                "new_hash": new_hash
            })

            logger.info(f"Secret '{name}' rotated via emergency endpoint (old={old_hash}, new={new_hash})")

            return web.json_response({
                "status": "rotated",
                "secret_name": name,
                "new_hash": new_hash
            })

        except Exception as e:
            return web.json_response({"error": str(e)}, status=400)

    async def _handle_status(self, request: web.Request) -> web.Response:
        """Show rotation history (hashed — never reveals actual values)"""
        if not self._verify_admin(request):
            return web.json_response({"error": "Unauthorized"}, status=401)

        return web.json_response({
            "secrets": list(self._secrets.keys()),
            "rotation_log": self._rotation_log[-10:]  # Last 10 rotations
        })

    async def start(self, port: int = 9090):
        """Start the rotation controller on an internal port"""
        runner = web.AppRunner(self._app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", port)  # Local only — not exposed publicly
        await site.start()
        logger.info(f"Emergency rotation controller on 127.0.0.1:{port}")

# Setup:
rotation_ctrl = EmergencyRotationController(
    admin_token=os.getenv("ADMIN_ROTATION_TOKEN")
)
rotation_ctrl.set_secret("ANTHROPIC_API_KEY", os.getenv("ANTHROPIC_API_KEY"))

# Usage in agent:
def get_anthropic_key() -> str:
    return rotation_ctrl.get_secret("ANTHROPIC_API_KEY")

# Emergency rotation (from ops team):
# curl -X POST http://agent-pod:9090/rotate-secret \
#   -H "X-Admin-Token: $ADMIN_TOKEN" \
#   -d '{"secret_name": "ANTHROPIC_API_KEY", "new_value": "sk-ant-new-key..."}'
```

### Option 3: Short-lived tokens — auto-expire makes rotation irrelevant

```python
import time
import threading
import httpx
import os
from typing import Optional

class SelfExpiringTokenManager:
    """
    Manages short-lived access tokens that auto-expire.
    Leaking a short-lived token is low risk — it expires soon anyway.
    Automatically refreshes before expiry.
    """

    def __init__(
        self,
        refresh_url: str,
        client_id: str,
        client_secret: str,
        token_lifetime_seconds: int = 3600,
        refresh_before_expiry_seconds: int = 300
    ):
        self.refresh_url = refresh_url
        self.client_id = client_id
        self.client_secret = client_secret
        self.token_lifetime = token_lifetime_seconds
        self.refresh_buffer = refresh_before_expiry_seconds

        self._token: Optional[str] = None
        self._expires_at: float = 0
        self._lock = threading.Lock()
        self._fetch_token()

    def _fetch_token(self):
        """Fetch a new short-lived token"""
        with httpx.Client() as client:
            response = client.post(
                self.refresh_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.client_id,
                    "client_secret": self.client_secret
                },
                timeout=10.0
            )
            response.raise_for_status()
            data = response.json()
            self._token = data["access_token"]
            # Token expires in `expires_in` seconds from now
            expires_in = data.get("expires_in", self.token_lifetime)
            self._expires_at = time.time() + expires_in
            print(f"Token fetched — expires in {expires_in}s ({expires_in//60}min)")

    @property
    def token(self) -> str:
        """Get current token, refreshing if near expiry"""
        with self._lock:
            if time.time() > self._expires_at - self.refresh_buffer:
                print("Token near expiry — refreshing")
                self._fetch_token()
            return self._token

# A leaked short-lived token is far less dangerous than a leaked long-lived key.
# If a token appears in logs, it expires in 1 hour — attackers have a narrow window.
token_manager = SelfExpiringTokenManager(
    refresh_url="https://auth.example.com/oauth/token",
    client_id=os.getenv("CLIENT_ID"),
    client_secret=os.getenv("CLIENT_SECRET"),
    token_lifetime_seconds=3600,
    refresh_before_expiry_seconds=300
)
```

### Option 4: Secret exposure detector — scan outputs before sending

```python
import re
from typing import Optional

# Patterns for common API key formats
SECRET_PATTERNS = [
    (r"sk-ant-[a-zA-Z0-9-_]{32,}", "Anthropic API key"),
    (r"sk-[a-zA-Z0-9]{48}", "OpenAI API key"),
    (r"AIza[0-9A-Za-z\-_]{35}", "Google API key"),
    (r"[a-zA-Z0-9]{32}\.secret\.[a-zA-Z0-9]{32}", "Generic secret"),
    (r"Bearer [a-zA-Z0-9\-_.~+/]+=*", "Bearer token"),
    (r"(?i)api[_-]?key[\"':]?\s*[=:]\s*[\"']?([a-zA-Z0-9_\-]{20,})", "Generic API key"),
    (r"(?i)secret[\"':]?\s*[=:]\s*[\"']?([a-zA-Z0-9_\-]{20,})", "Generic secret field"),
]

def scan_for_secrets(text: str) -> list[dict]:
    """
    Scan text for potential secret exposure.
    Returns list of found secrets with their type and redacted preview.
    """
    found = []
    for pattern, label in SECRET_PATTERNS:
        matches = re.findall(pattern, text)
        for match in matches:
            secret = match if isinstance(match, str) else match[0]
            if len(secret) >= 20:
                # Show first 4 and last 4 chars only
                redacted = f"{secret[:4]}...{secret[-4:]}"
                found.append({
                    "type": label,
                    "redacted": redacted,
                    "position": text.find(secret)
                })
    return found

def redact_secrets(text: str) -> str:
    """Replace detected secrets with [REDACTED]"""
    redacted = text
    for pattern, label in SECRET_PATTERNS:
        def replace_match(m):
            matched = m.group(0)
            return f"[REDACTED:{label}]"
        redacted = re.sub(pattern, replace_match, redacted)
    return redacted

def safe_log(message: str, level: str = "info"):
    """Log message with secrets redacted"""
    import logging
    logger = logging.getLogger(__name__)
    safe_message = redact_secrets(message)
    getattr(logger, level)(safe_message)

def check_agent_output_for_leaks(agent_response: str) -> Optional[str]:
    """
    Check agent response before sending to user.
    Returns warning if secrets detected.
    """
    found = scan_for_secrets(agent_response)
    if found:
        secret_types = [s["type"] for s in found]
        return (
            f"WARNING: Agent response may contain {len(found)} secret(s): {secret_types}. "
            f"Response blocked — investigate and redact before sending."
        )
    return None

# Middleware to check all agent outputs:
async def agent_output_middleware(response: str) -> str:
    leak_warning = check_agent_output_for_leaks(response)
    if leak_warning:
        import logging
        logging.getLogger(__name__).critical(leak_warning)
        return "[Response redacted: potential credential exposure detected. Please contact your administrator.]"
    return response
```

### Option 5: Kubernetes secret rotation with auto-reload

```yaml
# kubernetes/secret-rotation.yaml
# Use external-secrets-operator or sealed-secrets for automatic rotation

apiVersion: external-secrets.io/v1beta1
kind: ExternalSecret
metadata:
  name: anthropic-api-key
spec:
  refreshInterval: "5m"   # Check for rotation every 5 minutes
  secretStoreRef:
    name: aws-secrets-manager
    kind: ClusterSecretStore
  target:
    name: anthropic-credentials
    creationPolicy: Owner
  data:
  - secretKey: api-key
    remoteRef:
      key: prod/anthropic/api-key
---
# Mount as file (not env var) for hot-reload:
apiVersion: apps/v1
kind: Deployment
spec:
  template:
    spec:
      volumes:
      - name: secrets
        secret:
          secretName: anthropic-credentials
          # File is updated when secret rotates — no pod restart needed
      containers:
      - name: agent
        volumeMounts:
        - name: secrets
          mountPath: /secrets
          readOnly: true
        env:
        # Don't use env vars for secrets — they require restart to update
        # - name: ANTHROPIC_API_KEY
        #   valueFrom: secretKeyRef: ...   # BAD: requires restart

        # Instead, read from file path — hot-reload capable
        - name: ANTHROPIC_KEY_PATH
          value: /secrets/api-key
```

```python
# Agent reads key from file — picks up rotation without restart
import os
from pathlib import Path

def get_api_key() -> str:
    """Read API key from mounted file — auto-updates on rotation"""
    key_path = os.getenv("ANTHROPIC_KEY_PATH", "/secrets/api-key")
    try:
        return Path(key_path).read_text().strip()
    except FileNotFoundError:
        return os.getenv("ANTHROPIC_API_KEY", "")  # Fallback to env var

# Always call get_api_key() when making requests — never cache it
def make_anthropic_request(prompt: str) -> str:
    import anthropic
    client = anthropic.Anthropic(api_key=get_api_key())  # Fresh on each call
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text
```

### Option 6: Rotation runbook — documented emergency procedure

```python
ROTATION_RUNBOOK = """
# Emergency API Key Rotation Runbook

## Trigger
- Secret scanner alert
- Key found in logs/git/error messages
- Suspicious API usage detected
- Any other exposure event

## Step 1: Assess (2 minutes)
- Which key was exposed? (ANTHROPIC_API_KEY, DATABASE_URL, etc.)
- When was it exposed? (git blame, log timestamp)
- Is there evidence of unauthorized use? (check API dashboard)

## Step 2: Generate new key (1 minute)
- Go to the relevant API provider dashboard
- Generate a new key
- Save it to your password manager immediately
- Do NOT store it in git, Slack, email, or notes

## Step 3: Update secrets manager (2 minutes)
aws secretsmanager put-secret-value \\
  --secret-id prod/anthropic/api-key \\
  --secret-string "sk-ant-new-key-here"

## Step 4: Trigger hot-reload (1 minute)
# If using RotatableSecret (polls every 60s — wait or force):
curl -X POST http://agent-pod:9090/rotate-secret \\
  -H "X-Admin-Token: $ADMIN_ROTATION_TOKEN" \\
  -d '{"secret_name": "ANTHROPIC_API_KEY", "new_value": "sk-ant-new-key-here"}'

# Verify it took effect:
curl http://agent-pod:9090/secret-status \\
  -H "X-Admin-Token: $ADMIN_ROTATION_TOKEN"

## Step 5: Revoke old key (2 minutes)
- Go to API provider dashboard
- Revoke/delete the old key
- Verify revocation by attempting a request with the old key

## Step 6: Post-incident (15 minutes)
- Search logs for the exposed key value
- Determine root cause (where/how did it leak?)
- Update .gitignore, log filters, or error handling as needed
- Document in incident log

## Total time: < 10 minutes (if runbook is followed)
"""

def print_rotation_runbook():
    print(ROTATION_RUNBOOK)
```

## Rotation Design Patterns

| Pattern | Rotation Time | Requires Restart | Risk if Leaked |
|---|---|---|---|
| Hardcoded in code | Hours (new deploy) | Yes | High — never expires |
| Env var at startup | Minutes (redeploy) | Yes | High — never expires |
| Secrets manager + hot-reload | Seconds | No | Medium |
| File-mounted secrets (K8s) | Minutes (file update) | No | Medium |
| Short-lived tokens (1 hour TTL) | Automatic | No | Low — self-expires |
| Emergency rotation endpoint | Seconds | No | Medium |

## Expected Token Savings
Exposed key used by attacker → unexpected charges, investigation, incident response: incalculable cost
Zero-downtime rotation within minutes of detection: 0 exposure window

## Environment
- Any production agent with long-running secrets; rotation capability is especially critical for agents that log extensively, generate error messages with context, or run in multi-tenant environments — design for rotation on day one, not after an incident
- Source: direct experience; the most damaging security incidents in production agents involve keys that were detected as exposed but couldn't be rotated quickly due to missing hot-reload infrastructure
