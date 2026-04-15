---
layout: solution
title: "Agent Doesn't Rotate API Keys Before Expiry"
category: auth
description: "Agent uses a single long-lived API key that expires without warning, causing sudden auth failures and service outages with no graceful degradation."
tags: [auth, api-keys, rotation, expiry, reliability, secrets-management]
---

## Symptom

Agent fails with auth errors after a key expires silently:

```
[2026-04-15 03:14:00] Agent processing 500 queued requests...
[2026-04-15 03:14:02] ERROR: 401 Unauthorized — API key expired
[2026-04-15 03:14:02] All 500 requests failed
[2026-04-15 03:14:02] Queue depth: 500 messages unprocessed

# Root cause: API key was valid for 90 days, created 2026-01-15
# No rotation reminder, no monitoring, no backup key
# On-call engineer spent 47 minutes diagnosing the "mystery auth failure"
```

Or the subtler version: key expiry on a weekend at 3 AM with no alerting, discovered Monday morning when users report the agent is "broken."

## Root Cause

API keys are treated as static config values rather than rotating secrets. Without expiry monitoring, automatic rotation, or fallback keys, any key expiry causes an immediate service outage. The failure is especially insidious because the key works perfectly until the exact second it expires — making proactive detection difficult without explicit expiry tracking.

## Fix

---

### Option 1: Expiry-Aware API Key Manager with Advance Warning

Track key expiry dates and emit warnings when approaching expiry. Fail loudly before expiry so humans have time to rotate.

```python
import time
import anthropic
from datetime import datetime, timedelta
from dataclasses import dataclass

@dataclass
class ManagedKey:
    name: str
    value: str
    expires_at: datetime | None  # None = never expires
    created_at: datetime = None

    def __post_init__(self):
        if self.created_at is None:
            self.created_at = datetime.now()

    @property
    def days_until_expiry(self) -> float | None:
        if self.expires_at is None:
            return None
        return (self.expires_at - datetime.now()).total_seconds() / 86400

    @property
    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        return datetime.now() >= self.expires_at

    @property
    def is_warning_zone(self) -> bool:
        days = self.days_until_expiry
        return days is not None and days <= 14

class KeyManager:
    def __init__(self, warn_days: int = 14, critical_days: int = 3):
        self.warn_days = warn_days
        self.critical_days = critical_days
        self._keys: list[ManagedKey] = []

    def register(self, key: ManagedKey) -> None:
        self._keys.append(key)
        self._check_key(key)

    def _check_key(self, key: ManagedKey) -> None:
        if key.is_expired:
            raise RuntimeError(f"Key '{key.name}' EXPIRED at {key.expires_at}. Rotate immediately.")
        days = key.days_until_expiry
        if days is None:
            return
        if days <= self.critical_days:
            print(f"[CRITICAL] Key '{key.name}' expires in {days:.1f} days! Rotate NOW.")
        elif days <= self.warn_days:
            print(f"[WARNING] Key '{key.name}' expires in {days:.1f} days. Schedule rotation.")

    def get_active_key(self, name: str) -> ManagedKey:
        for key in self._keys:
            if key.name == name:
                self._check_key(key)
                return key
        raise KeyError(f"Key '{name}' not found")

    def check_all(self) -> list[str]:
        """Return list of warnings. Call this on startup and on a cron schedule."""
        warnings = []
        for key in self._keys:
            if key.is_expired:
                warnings.append(f"EXPIRED: {key.name}")
            elif key.is_warning_zone:
                warnings.append(f"EXPIRING SOON ({key.days_until_expiry:.0f}d): {key.name}")
        return warnings

import os

manager = KeyManager(warn_days=14, critical_days=3)
manager.register(ManagedKey(
    name="anthropic_primary",
    value=os.environ.get("ANTHROPIC_API_KEY", "sk-live-placeholder"),
    expires_at=datetime.now() + timedelta(days=5),  # simulate near-expiry
))

warnings = manager.check_all()
for w in warnings:
    print(f"Key status: {w}")

# Use in agent
active_key = manager.get_active_key("anthropic_primary")
client = anthropic.Anthropic(api_key=active_key.value)
```

**Expected Token Savings:** Zero direct token savings — this is reliability engineering. Prevents outages that require manual intervention (typically 30-90 minutes of engineer time) and queue drain of accumulated requests. ROI: prevents one 500-request failure event.
**Environment:** Run `check_all()` on process startup and via a daily cron job. Alert via Slack/PagerDuty when warnings are detected. Store expiry dates in a config file or secrets manager alongside the key value.

---

### Option 2: Active/Standby Key Pair with Automatic Failover

Maintain two keys simultaneously: one active, one standby. When the active key fails auth, automatically switch to standby and alert for rotation.

```python
import os
import time
import anthropic
from dataclasses import dataclass, field

@dataclass
class KeyPair:
    primary: str
    standby: str
    _active_is_primary: bool = True
    _primary_failures: int = 0
    _standby_failures: int = 0
    _failover_time: float | None = None

    @property
    def active_key(self) -> str:
        return self.primary if self._active_is_primary else self.standby

    @property
    def is_using_standby(self) -> bool:
        return not self._active_is_primary

    def record_auth_failure(self, key: str) -> None:
        if key == self.primary:
            self._primary_failures += 1
        else:
            self._standby_failures += 1

    def failover(self) -> str:
        """Switch to standby. Returns the new active key."""
        if self._active_is_primary:
            self._active_is_primary = False
            self._failover_time = time.time()
            print(f"[FAILOVER] Switched to standby key. Primary key likely expired/invalid.")
            print("[ACTION REQUIRED] Rotate the primary key immediately.")
            return self.standby
        else:
            raise RuntimeError("Both primary and standby keys are failing. Manual intervention required.")

_key_pair = KeyPair(
    primary=os.environ.get("ANTHROPIC_API_KEY_PRIMARY", "sk-live-primary"),
    standby=os.environ.get("ANTHROPIC_API_KEY_STANDBY", "sk-live-standby"),
)

def call_with_failover(prompt: str, max_retries: int = 2) -> str:
    for attempt in range(max_retries):
        current_key = _key_pair.active_key
        client = anthropic.Anthropic(api_key=current_key)
        try:
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=256,
                messages=[{"role": "user", "content": prompt}],
            )
            return response.content[0].text
        except anthropic.AuthenticationError as e:
            print(f"Auth failure on {'primary' if _key_pair._active_is_primary else 'standby'} key: {e}")
            _key_pair.record_auth_failure(current_key)
            if attempt < max_retries - 1:
                _key_pair.failover()
            else:
                raise RuntimeError("All keys exhausted") from e

result = call_with_failover("What is 2 + 2?")
print(result)

if _key_pair.is_using_standby:
    print(f"WARNING: Running on standby key since {_key_pair._failover_time}")
```

**Expected Token Savings:** Failover to standby key preserves 100% of in-flight requests that would otherwise fail. Without failover, a key expiry event causes total request loss until manual rotation (30-90 min). With failover: zero requests lost, one alert generated.
**Environment:** Provision both keys with staggered expiry dates (e.g., 90-day cycle, primary expires day 90, standby expires day 135). Rotate the primary immediately after any failover event. Store keys in separate environment variables.

---

### Option 3: HashiCorp Vault or AWS Secrets Manager — Dynamic Key Retrieval

Fetch API keys from a secrets manager at runtime instead of loading from environment. The secrets manager handles rotation automatically.

```python
import os
import time
import anthropic

# Key cache: avoid fetching on every request
_key_cache: dict[str, tuple[str, float]] = {}  # secret_path → (value, fetched_at)
KEY_CACHE_TTL = 300  # Re-fetch every 5 minutes

def get_api_key(secret_path: str = "anthropic/api-key") -> str:
    """Fetch from secrets manager with local caching."""
    cached = _key_cache.get(secret_path)
    if cached and time.time() - cached[1] < KEY_CACHE_TTL:
        return cached[0]

    # AWS Secrets Manager example
    try:
        import boto3
        client = boto3.client("secretsmanager")
        response = client.get_secret_value(SecretId=secret_path)
        key = response["SecretString"]
        _key_cache[secret_path] = (key, time.time())
        return key
    except ImportError:
        pass

    # HashiCorp Vault example
    try:
        import hvac
        vault = hvac.Client(url=os.environ.get("VAULT_ADDR", "http://localhost:8200"))
        vault.auth.token.create(token=os.environ.get("VAULT_TOKEN", ""))
        secret = vault.secrets.kv.read_secret_version(path=secret_path)
        key = secret["data"]["data"]["api_key"]
        _key_cache[secret_path] = (key, time.time())
        return key
    except ImportError:
        pass

    # Fallback to environment (dev)
    key = os.environ.get("ANTHROPIC_API_KEY", "sk-live-placeholder")
    _key_cache[secret_path] = (key, time.time())
    return key

def get_client() -> anthropic.Anthropic:
    """Get client with freshly-fetched key."""
    return anthropic.Anthropic(api_key=get_api_key())

# Rotation-aware call: if auth fails, clear cache and retry once with fresh key
def call_with_rotation_awareness(prompt: str) -> str:
    for attempt in range(2):
        try:
            client = get_client()
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=256,
                messages=[{"role": "user", "content": prompt}],
            )
            return response.content[0].text
        except anthropic.AuthenticationError:
            if attempt == 0:
                print("Auth error — clearing key cache and retrying with fresh key")
                _key_cache.clear()
            else:
                raise

result = call_with_rotation_awareness("Hello!")
print(result[:100])
```

**Expected Token Savings:** Secrets manager handles rotation automatically — the agent always gets the current key without any code changes or restarts. Zero outage events from key expiry. Auth failures automatically trigger a cache-clear and fresh fetch, recovering in <1 second.
**Environment:** AWS Secrets Manager supports automatic rotation with Lambda functions. Configure rotation schedule (30-90 days) in the secrets manager console. `KEY_CACHE_TTL=300` means the agent picks up a rotated key within 5 minutes without restart.

---

### Option 4: Proactive Expiry Test on Startup

On every process start, make a minimal test API call and verify the response includes a valid auth state. Fail fast with a clear error message before accepting any user requests.

```python
import os
import sys
import anthropic

def verify_api_key_on_startup(key: str, model: str = "claude-haiku-4-5-20251001") -> dict:
    """
    Make a minimal test call on startup.
    Returns: {"valid": bool, "error": str|None, "rate_limit_remaining": int|None}
    """
    client = anthropic.Anthropic(api_key=key)
    try:
        response = client.messages.create(
            model=model,
            max_tokens=1,
            messages=[{"role": "user", "content": "hi"}],
        )
        # Key is valid
        return {
            "valid": True,
            "error": None,
            "model": response.model,
            "input_tokens_used": response.usage.input_tokens,
        }
    except anthropic.AuthenticationError as e:
        return {"valid": False, "error": f"Auth failed: {e}"}
    except anthropic.RateLimitError as e:
        # Rate limited but key is valid
        return {"valid": True, "error": None, "note": f"Rate limited: {e}"}
    except anthropic.APIError as e:
        return {"valid": False, "error": f"API error: {e}"}

def startup_check() -> None:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        print("FATAL: ANTHROPIC_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    if not key.startswith("sk-"):
        print(f"WARNING: API key doesn't match expected format (sk-...): {key[:10]}...", file=sys.stderr)

    print("Verifying API key on startup...")
    status = verify_api_key_on_startup(key)

    if not status["valid"]:
        print(f"FATAL: API key verification failed: {status['error']}", file=sys.stderr)
        print("Action required: rotate ANTHROPIC_API_KEY before restarting", file=sys.stderr)
        sys.exit(1)

    print(f"API key verified OK (model={status.get('model')})")

    if status.get("note"):
        print(f"Note: {status['note']}")

# In your main entrypoint:
startup_check()

# Now safe to start accepting requests
client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
response = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=128,
    messages=[{"role": "user", "content": "What is 3 + 3?"}],
)
print(response.content[0].text)
```

**Expected Token Savings:** Startup test costs 1 token (max_tokens=1). Prevents silent failures where a bad key causes all requests to fail after the service has already accepted user traffic. Especially valuable in containerised deployments where a new pod with an expired key would silently fail all requests.
**Environment:** Add `startup_check()` to your application entrypoint (before uvicorn, gunicorn, etc. begins serving). In Docker: run in ENTRYPOINT so container exits with non-zero code if key is invalid, preventing readiness probe from passing.

---

### Option 5: Key Expiry Monitoring with Automated Rotation Workflow

Monitor expiry via a scheduled job and trigger a rotation workflow (GitHub Actions, AWS Lambda, or similar) when approaching expiry.

```python
import os
import json
import time
from datetime import datetime, timedelta
from pathlib import Path
import anthropic

KEY_METADATA_FILE = Path(".key_metadata.json")

def load_key_metadata() -> dict:
    if KEY_METADATA_FILE.exists():
        return json.loads(KEY_METADATA_FILE.read_text())
    return {}

def save_key_metadata(metadata: dict) -> None:
    KEY_METADATA_FILE.write_text(json.dumps(metadata, indent=2))

def register_key(key_name: str, expires_at: str | None, notes: str = "") -> None:
    """Register a key's expiry metadata. Call this when adding a new key."""
    metadata = load_key_metadata()
    metadata[key_name] = {
        "expires_at": expires_at,  # ISO format or None
        "registered_at": datetime.now().isoformat(),
        "notes": notes,
        "rotation_triggered": False,
    }
    save_key_metadata(metadata)
    print(f"Registered key '{key_name}' expiring at {expires_at or 'never'}")

def check_and_alert(warn_days: int = 14) -> list[dict]:
    """Run this daily via cron. Returns list of keys needing attention."""
    metadata = load_key_metadata()
    alerts = []
    now = datetime.now()

    for key_name, info in metadata.items():
        if not info.get("expires_at"):
            continue
        expires_at = datetime.fromisoformat(info["expires_at"])
        days_left = (expires_at - now).days

        if days_left < 0:
            alerts.append({"key": key_name, "level": "CRITICAL", "message": "EXPIRED"})
        elif days_left <= 3:
            alerts.append({"key": key_name, "level": "CRITICAL",
                          "message": f"Expires in {days_left} days"})
        elif days_left <= warn_days:
            alerts.append({"key": key_name, "level": "WARNING",
                          "message": f"Expires in {days_left} days"})

    return alerts

def trigger_rotation_workflow(key_name: str) -> None:
    """Trigger an automated rotation workflow. Adapt to your CI/CD system."""
    # GitHub Actions example:
    # import requests
    # requests.post(
    #     f"https://api.github.com/repos/{OWNER}/{REPO}/actions/workflows/rotate-key.yml/dispatches",
    #     json={"ref": "main", "inputs": {"key_name": key_name}},
    #     headers={"Authorization": f"Bearer {GITHUB_TOKEN}"},
    # )
    print(f"[ROTATION TRIGGER] Initiating rotation workflow for '{key_name}'")
    # Log the trigger
    metadata = load_key_metadata()
    if key_name in metadata:
        metadata[key_name]["rotation_triggered"] = True
        metadata[key_name]["rotation_triggered_at"] = datetime.now().isoformat()
        save_key_metadata(metadata)

def daily_rotation_check() -> None:
    """Entry point for daily cron job."""
    alerts = check_and_alert(warn_days=14)
    if not alerts:
        print("All keys healthy")
        return
    for alert in alerts:
        msg = f"[{alert['level']}] Key '{alert['key']}': {alert['message']}"
        print(msg)
        # Send to monitoring system
        if alert["level"] == "CRITICAL":
            trigger_rotation_workflow(alert["key"])
            # Also: send PagerDuty/Slack alert

# Example setup
register_key(
    "anthropic_production",
    expires_at=(datetime.now() + timedelta(days=10)).isoformat(),
    notes="Primary production key, rotate every 90 days",
)
daily_rotation_check()
```

**Expected Token Savings:** Automated rotation prevents outages — zero token loss from auth failures. The 14-day warning window gives ample time to rotate before expiry. Rotation workflow integration means no manual intervention needed for routine rotation.
**Environment:** Run `daily_rotation_check()` via cron or a cloud scheduler (AWS EventBridge, GitHub Actions schedule). Store key metadata in a database or config file tracked in version control (without the key values themselves).

---

### Option 6: Key Health Dashboard — Continuous Monitoring with Synthetic Probes

Run continuous synthetic probes against the API key. Alert immediately on auth degradation, well before hard expiry.

```python
import asyncio
import time
import os
from dataclasses import dataclass, field
import anthropic

@dataclass
class KeyHealthRecord:
    key_name: str
    check_interval_seconds: float = 300  # Check every 5 minutes
    consecutive_failures: int = 0
    last_success_at: float = field(default_factory=time.time)
    last_failure_at: float | None = None
    last_error: str = ""

class KeyHealthMonitor:
    def __init__(self):
        self._records: dict[str, KeyHealthRecord] = {}
        self._client: anthropic.AsyncAnthropic = anthropic.AsyncAnthropic()

    def register(self, key_name: str, interval: float = 300) -> None:
        self._records[key_name] = KeyHealthRecord(key_name, interval)

    async def _probe(self, key_name: str, api_key: str) -> bool:
        try:
            client = anthropic.AsyncAnthropic(api_key=api_key)
            await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1,
                messages=[{"role": "user", "content": "ping"}],
            )
            return True
        except anthropic.AuthenticationError:
            return False
        except Exception:
            return True  # Non-auth errors don't indicate key expiry

    async def _check_key(self, key_name: str, api_key: str) -> None:
        record = self._records[key_name]
        ok = await self._probe(key_name, api_key)

        if ok:
            record.consecutive_failures = 0
            record.last_success_at = time.time()
        else:
            record.consecutive_failures += 1
            record.last_failure_at = time.time()
            record.last_error = "AuthenticationError"

            if record.consecutive_failures == 1:
                print(f"[WARN] Key '{key_name}' first auth failure — may be expiring")
            elif record.consecutive_failures >= 3:
                print(f"[ALERT] Key '{key_name}' failed {record.consecutive_failures} consecutive checks — ROTATE NOW")
                await self._send_alert(key_name, record)

    async def _send_alert(self, key_name: str, record: KeyHealthRecord) -> None:
        # Replace with your alerting integration
        print(f"PagerDuty/Slack alert: Key '{key_name}' is failing auth. "
              f"Last success: {time.ctime(record.last_success_at)}. Rotate immediately.")

    async def run_forever(self, keys: dict[str, str]) -> None:
        """Run continuous health monitoring. Pass {key_name: key_value}."""
        while True:
            for key_name, api_key in keys.items():
                if key_name in self._records:
                    asyncio.create_task(self._check_key(key_name, api_key))
            await asyncio.sleep(min(r.check_interval_seconds for r in self._records.values()))

    def health_report(self) -> dict:
        return {
            name: {
                "status": "OK" if rec.consecutive_failures == 0 else "FAILING",
                "consecutive_failures": rec.consecutive_failures,
                "last_success": time.ctime(rec.last_success_at),
            }
            for name, rec in self._records.items()
        }

# Comparison table
"""
| Approach | Detection Lead Time | Auto-Rotation | Requires Infra | Complexity |
|---|---|---|---|---|
| Option 1: Expiry tracking | Days in advance | No | No | Low |
| Option 2: Active/standby | Immediate (failover) | No | Dual keys | Low |
| Option 3: Secrets manager | Days (managed) | Yes | Yes (Vault/AWS) | Medium |
| Option 4: Startup probe | At deploy time | No | No | Low |
| Option 5: Cron monitoring | Days in advance | Triggered | Cron | Medium |
| Option 6: Continuous probe | Minutes | Alert | Background task | Medium |
"""

async def main():
    monitor = KeyHealthMonitor()
    monitor.register("anthropic_primary", interval=300)
    monitor.register("anthropic_standby", interval=600)

    # Single health check (in production: run_forever)
    await monitor._check_key("anthropic_primary", os.environ.get("ANTHROPIC_API_KEY", "sk-live-test"))
    print(monitor.health_report())

asyncio.run(main())
```

**Expected Token Savings:** Continuous probing catches auth degradation within minutes rather than hours. Each probe costs 1 API token every 5 minutes = 288 tokens/day — negligible. Prevents outages that each result in 100s to 1000s of failed requests × full token cost.
**Environment:** Run monitor as a sidecar process or background task. In Kubernetes: deploy as a separate health-check pod. Configure `check_interval_seconds` based on acceptable detection latency vs probe cost.
