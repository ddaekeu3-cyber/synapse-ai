---
layout: solution
title: "API Key Rotation Breaks Running Agents — 401 Errors After Key Change"
category: auth
description: "Team rotates the Anthropic API key for security. All running agents immediately start getting 401 errors. Agents have the old key hardcoded or cached at startup."
tags: [api-key, rotation, 401, authentication, zero-downtime, configuration]
---

# API Key Rotation Breaks Running Agents — 401 Errors After Key Change

## Symptom
- Team rotates API key → all agents immediately fail with 401
- Agents that were running for days/weeks suddenly stop working
- Restarting agents fixes it (they pick up new key from env var)
- But agents mid-task lose their work when forced to restart
- Key rotation required (security policy) but causes outage every time

## Root Cause
Agents read the API key once at startup and cache it. When the key is rotated:
1. The old key is revoked
2. Running agents still have the old key in memory
3. Every API call fails with 401 until the agent restarts with the new key

## Fix

### Option 1: Read key fresh from environment on each client creation

```python
import os, anthropic

# WRONG — reads key once at module import, cached forever
client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

# RIGHT — create client lazily, re-read key each time
def get_client() -> anthropic.Anthropic:
    """Create fresh client — reads current key from environment"""
    return anthropic.Anthropic()  # Reads ANTHROPIC_API_KEY env var each time

# Usage
response = get_client().messages.create(...)
```

### Option 2: Retry on 401 by re-reading the key

```python
import os, anthropic

_client = None

def get_client(force_refresh: bool = False) -> anthropic.Anthropic:
    global _client
    if _client is None or force_refresh:
        _client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return _client

async def api_call_with_key_refresh(messages: list, **kwargs):
    """Retry with fresh key on 401"""
    for attempt in range(2):
        try:
            return get_client().messages.create(messages=messages, **kwargs)
        except anthropic.AuthenticationError:
            if attempt == 0:
                print("401: attempting key refresh...")
                get_client(force_refresh=True)  # Re-read from environment
            else:
                raise  # Second failure — key itself is wrong
```

### Option 3: Load key from secrets manager with TTL

```python
import boto3, time
from functools import lru_cache

class RotatingSecretClient:
    def __init__(self, secret_name: str, region: str = "us-east-1", ttl: int = 300):
        self.secret_name = secret_name
        self.region = region
        self.ttl = ttl  # Re-read secret every 5 minutes
        self._key = None
        self._fetched_at = 0

    def get_api_key(self) -> str:
        if time.time() - self._fetched_at > self.ttl:
            client = boto3.client("secretsmanager", region_name=self.region)
            response = client.get_secret_value(SecretId=self.secret_name)
            import json
            self._key = json.loads(response["SecretString"])["ANTHROPIC_API_KEY"]
            self._fetched_at = time.time()
            print(f"API key refreshed from secrets manager")
        return self._key

    def get_anthropic_client(self):
        import anthropic
        return anthropic.Anthropic(api_key=self.get_api_key())

secret_client = RotatingSecretClient("prod/anthropic/api_key")
```

### Option 4: Zero-downtime rotation with overlap period

```python
# Rotation strategy: create new key, allow overlap, then revoke old key

# Step 1: Create new key (keep old key active)
# In Anthropic console: add new key

# Step 2: Update secret in secrets manager
aws_client = boto3.client("secretsmanager")
aws_client.put_secret_value(
    SecretId="prod/anthropic/api_key",
    SecretString='{"ANTHROPIC_API_KEY": "sk-ant-NEW-KEY"}'
)

# Step 3: Wait for running agents to pick up new key via TTL refresh
# (e.g., wait 5 minutes if TTL is 300s)
time.sleep(300)

# Step 4: Verify agents are working with new key
# (check metrics/logs for successful API calls)

# Step 5: Revoke old key in Anthropic console
# Now safe — all agents use new key
```

### Option 5: Watch for config file changes

```python
import os, time, threading, anthropic

class ConfigWatcher:
    """Watch environment/config file for key rotation"""
    def __init__(self, env_var: str = "ANTHROPIC_API_KEY", poll_interval: int = 60):
        self.env_var = env_var
        self.poll_interval = poll_interval
        self._current_key = os.environ.get(env_var)
        self._client = anthropic.Anthropic(api_key=self._current_key)
        self._lock = threading.Lock()
        self._start_watching()

    def _start_watching(self):
        def watch():
            while True:
                time.sleep(self.poll_interval)
                new_key = os.environ.get(self.env_var)
                if new_key != self._current_key:
                    print(f"API key changed, updating client...")
                    with self._lock:
                        self._current_key = new_key
                        self._client = anthropic.Anthropic(api_key=new_key)
        t = threading.Thread(target=watch, daemon=True)
        t.start()

    def get_client(self) -> anthropic.Anthropic:
        with self._lock:
            return self._client

config = ConfigWatcher()
# Usage: config.get_client().messages.create(...)
```

### Option 6: Graceful handling during rotation window

```python
import anthropic, time

async def resilient_api_call(messages: list, max_rotation_wait: int = 120):
    """Handle 401 during key rotation — wait for new key to propagate"""
    start = time.time()

    while True:
        try:
            client = anthropic.Anthropic()  # Reads current env var
            return client.messages.create(messages=messages, model="claude-sonnet-4-6", max_tokens=1024)

        except anthropic.AuthenticationError as e:
            elapsed = time.time() - start
            if elapsed > max_rotation_wait:
                raise RuntimeError(f"Auth failed for {max_rotation_wait}s — key may be revoked") from e

            print(f"Auth failure (key may be rotating). Waiting 10s... ({elapsed:.0f}s elapsed)")
            time.sleep(10)
            # Loop: will re-read env var on next iteration
```

## Key Rotation Runbook

| Step | Action | Notes |
|---|---|---|
| 1 | Create new API key | Keep old key active |
| 2 | Deploy new key to secrets manager | Don't update running agents yet |
| 3 | Wait for TTL expiry | Running agents auto-refresh |
| 4 | Verify all agents using new key | Check success metrics |
| 5 | Revoke old key | Safe — all agents migrated |

## Expected Token Savings
Not about token savings — preventing auth outages during mandatory key rotation.

## Environment
- Production agents with security-mandated key rotation policies
- Source: direct experience with enterprise security compliance requirements
