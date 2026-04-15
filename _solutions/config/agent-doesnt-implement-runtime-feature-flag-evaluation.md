---
layout: solution
title: "Agent Doesn't Implement Runtime Feature Flag Evaluation"
category: config
description: "Agents that bake feature decisions into code require redeployment to change behavior. These patterns evaluate feature flags at runtime, enabling instant behavior changes, gradual rollouts, and A/B experiments without restarts."
tags: [feature-flags, runtime-config, gradual-rollout, ab-testing, dynamic-config, experimentation]
---

# Agent Doesn't Implement Runtime Feature Flag Evaluation

## The Problem

When an agent's behavior is hardcoded — which model to use, which tools to enable, which prompts to send — every change requires a code deploy and restart. This makes it impossible to:
- Roll back a bad model upgrade in seconds
- Enable a new feature for 10% of users without a deploy
- Run A/B experiments on prompt variants
- Disable expensive features under cost pressure instantly

Runtime feature flag evaluation decouples behavior decisions from code deployments.

---

## Option 1: File-Based Feature Flag Registry

Read flags from a JSON file on every request; editing the file takes effect immediately.

```python
import anthropic
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

client = anthropic.Anthropic()

FLAGS_FILE = Path("feature_flags.json")

@dataclass
class FlagCache:
    data: dict = field(default_factory=dict)
    loaded_at: float = 0.0
    ttl_seconds: float = 5.0  # Re-read file every 5 seconds

    def is_stale(self) -> bool:
        return time.time() - self.loaded_at > self.ttl_seconds

_cache = FlagCache()

def load_flags() -> dict:
    """Load flags from file, using cache to avoid constant disk reads."""
    if _cache.is_stale():
        if FLAGS_FILE.exists():
            with open(FLAGS_FILE) as f:
                _cache.data = json.load(f)
        _cache.loaded_at = time.time()
    return _cache.data

def is_enabled(flag_name: str, default: bool = False) -> bool:
    flags = load_flags()
    return flags.get(flag_name, {}).get("enabled", default)

def get_flag_value(flag_name: str, default=None):
    flags = load_flags()
    return flags.get(flag_name, {}).get("value", default)

def init_flags_file():
    """Create default flags file if it doesn't exist."""
    if not FLAGS_FILE.exists():
        defaults = {
            "use_extended_thinking": {"enabled": False, "value": None},
            "model_override": {"enabled": False, "value": "claude-sonnet-4-6"},
            "streaming_responses": {"enabled": True, "value": None},
            "verbose_logging": {"enabled": False, "value": None},
            "max_tokens_override": {"enabled": False, "value": 1024},
            "enable_tool_use": {"enabled": True, "value": None},
        }
        FLAGS_FILE.write_text(json.dumps(defaults, indent=2))
        print(f"Created {FLAGS_FILE}")

def run_agent_with_flags(user_message: str) -> str:
    """Agent that reads its behavior from runtime feature flags."""
    # Determine model
    model = "claude-haiku-4-5-20251001"  # default
    if is_enabled("model_override"):
        model = get_flag_value("model_override", model)

    # Determine max_tokens
    max_tokens = 512  # default
    if is_enabled("max_tokens_override"):
        max_tokens = get_flag_value("max_tokens_override", max_tokens)

    # Build params
    params = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": user_message}]
    }

    # Extended thinking flag
    if is_enabled("use_extended_thinking"):
        params["thinking"] = {"type": "enabled", "budget_tokens": 5000}

    if is_enabled("verbose_logging"):
        print(f"[FLAGS] model={model}, max_tokens={max_tokens}, "
              f"thinking={is_enabled('use_extended_thinking')}")

    response = client.messages.create(**params)
    return response.content[-1].text

# Usage
init_flags_file()
print("Current flags:", json.dumps(load_flags(), indent=2))
result = run_agent_with_flags("Explain quantum computing briefly.")
print(f"Response: {result[:200]}")
print("\n[Edit feature_flags.json to change behavior without restarting]")

# Expected Token Savings: Flags let you downgrade model tier instantly under cost pressure; no code change needed
# Environment: self-hosted agents, development environments, single-server deployments
```

---

## Option 2: SQLite-Backed Feature Flags with Audit Log

Store flags in SQLite for persistence, history, and rollback capability.

```python
import anthropic
import sqlite3
import json
import time
from contextlib import contextmanager
from datetime import datetime

client = anthropic.Anthropic()

DB_PATH = "agent_flags.db"

@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

def init_flags_db():
    """Initialize flags database with default flags."""
    with get_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS feature_flags (
                name TEXT PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 0,
                value TEXT,
                description TEXT,
                updated_at REAL NOT NULL,
                updated_by TEXT DEFAULT 'system'
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS flag_audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                flag_name TEXT NOT NULL,
                old_enabled INTEGER,
                new_enabled INTEGER,
                old_value TEXT,
                new_value TEXT,
                changed_by TEXT,
                changed_at REAL NOT NULL
            )
        """)

        # Insert defaults if not present
        defaults = [
            ("use_haiku_for_routing", 1, None, "Use Haiku for intent routing"),
            ("enable_tool_calls", 1, None, "Enable tool use"),
            ("model_tier", 0, "claude-sonnet-4-6", "Active model override"),
            ("max_context_turns", 0, "10", "Limit context window turns"),
            ("enable_streaming", 1, None, "Stream responses"),
        ]
        for name, enabled, value, desc in defaults:
            db.execute(
                "INSERT OR IGNORE INTO feature_flags VALUES (?, ?, ?, ?, ?, ?)",
                (name, enabled, value, desc, time.time(), "init")
            )

def get_flag(name: str, default_enabled: bool = False) -> tuple[bool, str | None]:
    """Get flag state. Returns (enabled, value)."""
    with get_db() as db:
        row = db.execute(
            "SELECT enabled, value FROM feature_flags WHERE name = ?", (name,)
        ).fetchone()
        if row:
            return bool(row["enabled"]), row["value"]
        return default_enabled, None

def set_flag(name: str, enabled: bool, value=None, changed_by: str = "api"):
    """Update a flag and record audit entry."""
    with get_db() as db:
        old = db.execute(
            "SELECT enabled, value FROM feature_flags WHERE name = ?", (name,)
        ).fetchone()
        old_enabled = old["enabled"] if old else None
        old_value = old["value"] if old else None

        db.execute("""
            INSERT INTO feature_flags (name, enabled, value, updated_at, updated_by)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                enabled = excluded.enabled,
                value = excluded.value,
                updated_at = excluded.updated_at,
                updated_by = excluded.updated_by
        """, (name, int(enabled), str(value) if value is not None else None, time.time(), changed_by))

        db.execute("""
            INSERT INTO flag_audit_log
            (flag_name, old_enabled, new_enabled, old_value, new_value, changed_by, changed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (name, old_enabled, int(enabled), old_value,
               str(value) if value is not None else None, changed_by, time.time()))

def list_flags() -> list[dict]:
    """List all flags with current state."""
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM feature_flags ORDER BY name"
        ).fetchall()
        return [dict(r) for r in rows]

def get_flag_history(name: str, limit: int = 10) -> list[dict]:
    """Get change history for a flag."""
    with get_db() as db:
        rows = db.execute("""
            SELECT * FROM flag_audit_log
            WHERE flag_name = ? ORDER BY changed_at DESC LIMIT ?
        """, (name, limit)).fetchall()
        return [dict(r) for r in rows]

def run_agent(user_message: str) -> dict:
    """Agent with SQLite-backed feature flag control."""
    use_haiku_routing, _ = get_flag("use_haiku_for_routing")
    model_override, model_value = get_flag("model_tier")
    streaming_enabled, _ = get_flag("enable_streaming")

    # Routing decision
    model = model_value if (model_override and model_value) else "claude-sonnet-4-6"

    if use_haiku_routing:
        # Quick Haiku routing to decide complexity
        route_resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=10,
            messages=[{
                "role": "user",
                "content": f"Is this complex (yes/no)? '{user_message[:100]}'"
            }]
        )
        is_complex = "yes" in route_resp.content[0].text.lower()
        if not is_complex:
            model = "claude-haiku-4-5-20251001"

    response = client.messages.create(
        model=model,
        max_tokens=512,
        messages=[{"role": "user", "content": user_message}]
    )

    return {
        "response": response.content[0].text,
        "model_used": model,
        "flags_applied": {
            "haiku_routing": use_haiku_routing,
            "model_override": model_override
        }
    }

# Usage
init_flags_db()

# Toggle a flag
set_flag("use_haiku_for_routing", True, changed_by="ops-team")
set_flag("model_tier", True, value="claude-haiku-4-5-20251001", changed_by="cost-optimization")

print("Current flags:")
for f in list_flags():
    print(f"  {f['name']}: enabled={bool(f['enabled'])}, value={f['value']}")

result = run_agent("What is the capital of France?")
print(f"\nModel used: {result['model_used']}")
print(f"Response: {result['response'][:100]}")

# Expected Token Savings: Flag to downgrade model saves 80% cost per call; audit log tracks who changed what and when
# Environment: production agents, cost control, team-operated deployments, SOC 2 compliant systems
```

---

## Option 3: Environment Variable Flag Evaluator with Gradual Rollout

Evaluate flags from environment variables with percentage-based rollout using user ID hashing.

```python
import anthropic
import hashlib
import os
from dataclasses import dataclass

client = anthropic.Anthropic()

@dataclass
class RolloutFlag:
    name: str
    enabled_pct: int  # 0-100; percentage of users for whom flag is enabled
    value: str | None = None

def parse_rollout_flags() -> dict[str, RolloutFlag]:
    """
    Parse flags from environment variables.
    Format: FLAG_<NAME>=<pct>:<value> or FLAG_<NAME>=<pct>
    Examples:
      FLAG_NEW_PROMPT=50:v2          # Enable for 50% of users, value="v2"
      FLAG_STREAMING=100             # Enable for all users
      FLAG_EXPERIMENTAL=10           # Enable for 10% of users
    """
    flags = {}
    for key, val in os.environ.items():
        if not key.startswith("FLAG_"):
            continue
        name = key[5:].lower()
        parts = val.split(":", 1)
        try:
            pct = int(parts[0])
            value = parts[1] if len(parts) > 1 else None
            flags[name] = RolloutFlag(name=name, enabled_pct=pct, value=value)
        except ValueError:
            pass
    return flags

def is_flag_enabled_for_user(
    flag: RolloutFlag,
    user_id: str
) -> tuple[bool, str | None]:
    """Deterministic rollout using user_id hash."""
    if flag.enabled_pct >= 100:
        return True, flag.value
    if flag.enabled_pct <= 0:
        return False, None

    # Hash user_id + flag_name for determinism
    hash_input = f"{user_id}:{flag.name}".encode()
    hash_int = int(hashlib.md5(hash_input).hexdigest(), 16)
    user_bucket = hash_int % 100  # 0-99

    enabled = user_bucket < flag.enabled_pct
    return enabled, (flag.value if enabled else None)

def evaluate_flags_for_user(user_id: str) -> dict:
    """Evaluate all environment flags for a specific user."""
    flags = parse_rollout_flags()
    evaluated = {}
    for name, flag in flags.items():
        enabled, value = is_flag_enabled_for_user(flag, user_id)
        evaluated[name] = {"enabled": enabled, "value": value, "rollout_pct": flag.enabled_pct}
    return evaluated

def run_agent_for_user(user_id: str, user_message: str) -> dict:
    """Run agent with flags evaluated per-user."""
    user_flags = evaluate_flags_for_user(user_id)

    # Model selection
    model = "claude-haiku-4-5-20251001"
    if user_flags.get("model_upgrade", {}).get("enabled"):
        model = user_flags["model_upgrade"].get("value", "claude-sonnet-4-6")

    # Prompt variant
    system_prompt = "You are a helpful assistant."
    if user_flags.get("new_prompt", {}).get("enabled"):
        variant = user_flags["new_prompt"].get("value", "v1")
        if variant == "v2":
            system_prompt = "You are a concise, expert assistant. Prioritize brevity."

    response = client.messages.create(
        model=model,
        max_tokens=512,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}]
    )

    return {
        "user_id": user_id,
        "model": model,
        "flags": user_flags,
        "response": response.content[0].text
    }

# Simulate environment-based flags (set these as real env vars in production)
os.environ["FLAG_MODEL_UPGRADE"] = "50:claude-sonnet-4-6"  # 50% get Sonnet
os.environ["FLAG_NEW_PROMPT"] = "25:v2"                    # 25% get v2 prompt

# Test rollout consistency
print("Flag rollout simulation:")
for user_id in ["user_001", "user_042", "user_100", "user_250", "user_999"]:
    result = run_agent_for_user(user_id, "Explain REST APIs briefly.")
    flags_active = {k: v for k, v in result["flags"].items() if v["enabled"]}
    print(f"  {user_id}: model={result['model']}, active_flags={list(flags_active.keys())}")

# Expected Token Savings: Gradual rollout of expensive models to 10% → 50% → 100%; limits cost impact of upgrades
# Environment: SaaS products, multi-tenant APIs, progressive feature rollout, A/B experiments
```

---

## Option 4: In-Memory Flag Store with HTTP Update Endpoint

Maintain flags in memory with a lightweight HTTP endpoint for real-time updates.

```python
import anthropic
import json
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from dataclasses import dataclass, field

client = anthropic.Anthropic()

@dataclass
class FlagStore:
    _flags: dict = field(default_factory=dict)
    _lock: threading.RLock = field(default_factory=threading.RLock)

    def get(self, name: str, default=None):
        with self._lock:
            flag = self._flags.get(name, {})
            if not flag.get("enabled", False):
                return default
            return flag.get("value", True)

    def is_enabled(self, name: str) -> bool:
        with self._lock:
            return self._flags.get(name, {}).get("enabled", False)

    def set(self, name: str, enabled: bool, value=None):
        with self._lock:
            self._flags[name] = {"enabled": enabled, "value": value}

    def all_flags(self) -> dict:
        with self._lock:
            return dict(self._flags)

# Global flag store
store = FlagStore()

# Initialize defaults
store.set("streaming", True)
store.set("model", True, "claude-haiku-4-5-20251001")
store.set("extended_thinking", False)
store.set("tool_use", True)

class FlagHandler(BaseHTTPRequestHandler):
    """Simple HTTP handler for flag management."""

    def log_message(self, format, *args):
        pass  # Suppress access logs

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/flags":
            self._json_response(200, store.all_flags())
        else:
            self._json_response(404, {"error": "Not found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/flags":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                data = json.loads(body)
                name = data["name"]
                enabled = data["enabled"]
                value = data.get("value")
                store.set(name, enabled, value)
                self._json_response(200, {"ok": True, "flag": name, "enabled": enabled})
            except (json.JSONDecodeError, KeyError) as e:
                self._json_response(400, {"error": str(e)})
        else:
            self._json_response(404, {"error": "Not found"})

    def _json_response(self, code: int, data: dict):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

def start_flag_server(port: int = 8765):
    """Start flag management server in background thread."""
    server = HTTPServer(("localhost", port), FlagHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"Flag server running on port {port}")
    print(f"  GET  http://localhost:{port}/flags")
    print(f"  POST http://localhost:{port}/flags  {{\"name\": \"...\", \"enabled\": true}}")
    return server

def run_agent(message: str) -> dict:
    """Agent controlled by in-memory feature flags."""
    model_value = store.get("model", "claude-haiku-4-5-20251001")
    model = model_value if isinstance(model_value, str) else "claude-haiku-4-5-20251001"

    params = {
        "model": model,
        "max_tokens": 512,
        "messages": [{"role": "user", "content": message}]
    }

    if store.is_enabled("extended_thinking"):
        params["thinking"] = {"type": "enabled", "budget_tokens": 3000}

    response = client.messages.create(**params)
    return {
        "response": response.content[0].text,
        "model": model,
        "active_flags": {k: v for k, v in store.all_flags().items() if v.get("enabled")}
    }

# Demo
server = start_flag_server(8765)

# Show initial state
print(f"\nInitial flags: {json.dumps(store.all_flags(), indent=2)}")

# Runtime flag change (simulating HTTP POST)
store.set("model", True, "claude-sonnet-4-6")
print("\n[Runtime update: upgraded model to Sonnet]")

result = run_agent("What is machine learning?")
print(f"Model used: {result['model']}")
print(f"Response: {result['response'][:150]}")

# Rollback (simulating POST)
store.set("model", True, "claude-haiku-4-5-20251001")
print("\n[Runtime rollback: downgraded to Haiku]")

# Expected Token Savings: Instant model downgrade via HTTP saves cost immediately; no restart = zero downtime
# Environment: containerized agents, microservices with ops dashboards, on-call incident response
```

---

## Option 5: Flag-Gated Tool Registry

Enable or disable specific tools at runtime via feature flags without redeploying.

```python
import anthropic
import json
from typing import Any

client = anthropic.Anthropic()

# All available tools
ALL_TOOLS = {
    "web_search": {
        "name": "web_search",
        "description": "Search the web for current information",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"]
        }
    },
    "calculator": {
        "name": "calculator",
        "description": "Evaluate mathematical expressions",
        "input_schema": {
            "type": "object",
            "properties": {"expression": {"type": "string"}},
            "required": ["expression"]
        }
    },
    "file_writer": {
        "name": "file_writer",
        "description": "Write content to a file",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"}
            },
            "required": ["path", "content"]
        }
    },
    "database_query": {
        "name": "database_query",
        "description": "Execute a database query",
        "input_schema": {
            "type": "object",
            "properties": {"sql": {"type": "string"}},
            "required": ["sql"]
        }
    }
}

# Feature flag → tool mapping
TOOL_FLAGS = {
    "web_search": "enable_web_search",
    "calculator": "enable_calculator",
    "file_writer": "enable_file_writer",
    "database_query": "enable_database_access",
}

# Simulated flag store (replace with real flag system)
_flag_overrides: dict[str, bool] = {
    "enable_web_search": True,
    "enable_calculator": True,
    "enable_file_writer": False,   # Disabled for safety
    "enable_database_access": False,  # Disabled for this user tier
}

def get_enabled_tools() -> list[dict]:
    """Return only tools whose feature flags are enabled."""
    enabled = []
    for tool_name, flag_name in TOOL_FLAGS.items():
        if _flag_overrides.get(flag_name, False):
            enabled.append(ALL_TOOLS[tool_name])
    return enabled

def handle_tool_call(tool_name: str, tool_input: dict) -> str:
    """Execute tool — only runs if flag was enabled when tools were assembled."""
    if tool_name == "calculator":
        try:
            result = eval(tool_input["expression"], {"__builtins__": {}})
            return str(result)
        except Exception as e:
            return f"Error: {e}"
    elif tool_name == "web_search":
        return f"[Simulated search results for: {tool_input['query']}]"
    elif tool_name == "file_writer":
        return f"[File write disabled by feature flag]"
    elif tool_name == "database_query":
        return f"[Database access disabled by feature flag]"
    return "Tool not implemented"

def run_agent_with_flagged_tools(user_message: str) -> dict:
    """Agent with dynamically assembled tool set based on feature flags."""
    enabled_tools = get_enabled_tools()
    tool_names = [t["name"] for t in enabled_tools]
    print(f"Enabled tools: {tool_names}")

    messages = [{"role": "user", "content": user_message}]
    params: dict[str, Any] = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 512,
        "messages": messages,
    }
    if enabled_tools:
        params["tools"] = enabled_tools

    response = client.messages.create(**params)

    # Handle tool use
    while response.stop_reason == "tool_use":
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                result = handle_tool_call(block.name, block.input)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result
                })

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})
        params["messages"] = messages
        response = client.messages.create(**params)

    text_blocks = [b for b in response.content if hasattr(b, 'text')]
    return {
        "response": text_blocks[0].text if text_blocks else "",
        "tools_available": tool_names,
        "stop_reason": response.stop_reason
    }

# Demo: toggle tool access at runtime
print("=== Default tool set ===")
result = run_agent_with_flagged_tools("Calculate 15 * 24 + 100")
print(f"Response: {result['response'][:200]}\n")

# Disable calculator at runtime
_flag_overrides["enable_calculator"] = False
print("=== After disabling calculator ===")
result = run_agent_with_flagged_tools("Calculate 15 * 24 + 100")
print(f"Available tools: {result['tools_available']}")
print(f"Response: {result['response'][:200]}")

# Expected Token Savings: Fewer tools in context = fewer tokens per call; disable expensive tools under budget pressure
# Environment: multi-tier SaaS, tool access by subscription plan, ops-controlled tool disabling
```

---

## Option 6: Hierarchical Flag Evaluation (Global → Tenant → User)

Evaluate flags through a priority hierarchy: user flags override tenant flags override global flags.

```python
import anthropic
import json
import time
from dataclasses import dataclass, field

client = anthropic.Anthropic()

@dataclass
class FlagLayer:
    """A single layer in the flag hierarchy."""
    name: str
    flags: dict = field(default_factory=dict)

    def get(self, flag_name: str) -> dict | None:
        return self.flags.get(flag_name)

class HierarchicalFlagEvaluator:
    """
    Evaluates flags through: USER > TENANT > GLOBAL
    More specific layers override less specific ones.
    """

    def __init__(self):
        self._global = FlagLayer("global")
        self._tenants: dict[str, FlagLayer] = {}
        self._users: dict[str, FlagLayer] = {}

    def set_global(self, flag: str, enabled: bool, value=None):
        self._global.flags[flag] = {"enabled": enabled, "value": value}

    def set_tenant(self, tenant_id: str, flag: str, enabled: bool, value=None):
        if tenant_id not in self._tenants:
            self._tenants[tenant_id] = FlagLayer(f"tenant:{tenant_id}")
        self._tenants[tenant_id].flags[flag] = {"enabled": enabled, "value": value}

    def set_user(self, user_id: str, flag: str, enabled: bool, value=None):
        if user_id not in self._users:
            self._users[user_id] = FlagLayer(f"user:{user_id}")
        self._users[user_id].flags[flag] = {"enabled": enabled, "value": value}

    def evaluate(self, flag: str, user_id: str, tenant_id: str) -> tuple[bool, any, str]:
        """
        Evaluate flag for a user+tenant context.
        Returns (enabled, value, source_layer).
        """
        # User layer (highest priority)
        user_layer = self._users.get(user_id)
        if user_layer:
            user_flag = user_layer.get(flag)
            if user_flag is not None:
                return user_flag["enabled"], user_flag.get("value"), f"user:{user_id}"

        # Tenant layer
        tenant_layer = self._tenants.get(tenant_id)
        if tenant_layer:
            tenant_flag = tenant_layer.get(flag)
            if tenant_flag is not None:
                return tenant_flag["enabled"], tenant_flag.get("value"), f"tenant:{tenant_id}"

        # Global layer (lowest priority)
        global_flag = self._global.get(flag)
        if global_flag is not None:
            return global_flag["enabled"], global_flag.get("value"), "global"

        return False, None, "default"

    def evaluate_all(self, user_id: str, tenant_id: str) -> dict:
        """Get all flags for a user+tenant context."""
        all_flag_names = set(self._global.flags.keys())
        if tenant_id in self._tenants:
            all_flag_names |= set(self._tenants[tenant_id].flags.keys())
        if user_id in self._users:
            all_flag_names |= set(self._users[user_id].flags.keys())

        result = {}
        for flag in all_flag_names:
            enabled, value, source = self.evaluate(flag, user_id, tenant_id)
            result[flag] = {"enabled": enabled, "value": value, "source": source}
        return result

def run_hierarchical_agent(
    evaluator: HierarchicalFlagEvaluator,
    user_id: str,
    tenant_id: str,
    message: str
) -> dict:
    """Run agent with hierarchically evaluated flags."""
    flags = evaluator.evaluate_all(user_id, tenant_id)

    # Model selection
    model_enabled, model_value, model_source = evaluator.evaluate("model", user_id, tenant_id)
    model = model_value if (model_enabled and model_value) else "claude-haiku-4-5-20251001"

    # Max tokens
    tokens_enabled, tokens_value, _ = evaluator.evaluate("max_tokens", user_id, tenant_id)
    max_tokens = int(tokens_value) if (tokens_enabled and tokens_value) else 256

    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": message}]
    )

    return {
        "user_id": user_id,
        "tenant_id": tenant_id,
        "model": model,
        "model_source": model_source,
        "max_tokens": max_tokens,
        "response": response.content[0].text[:150],
        "flag_summary": {k: v["source"] for k, v in flags.items()}
    }

# Setup hierarchy
evaluator = HierarchicalFlagEvaluator()

# Global defaults (all tenants)
evaluator.set_global("model", True, "claude-haiku-4-5-20251001")
evaluator.set_global("max_tokens", True, "256")
evaluator.set_global("enable_tools", False)

# Enterprise tenant gets Sonnet + higher token limit
evaluator.set_tenant("enterprise", "model", True, "claude-sonnet-4-6")
evaluator.set_tenant("enterprise", "max_tokens", True, "1024")
evaluator.set_tenant("enterprise", "enable_tools", True)

# Power user override within enterprise
evaluator.set_user("user_vip_001", "model", True, "claude-opus-4-6")

# Test hierarchy
print("Flag evaluation across contexts:")
for user_id, tenant_id in [
    ("user_basic", "starter"),
    ("user_enterprise", "enterprise"),
    ("user_vip_001", "enterprise"),
]:
    result = run_hierarchical_agent(evaluator, user_id, tenant_id, "Hi!")
    print(f"  {user_id} / {tenant_id}: model={result['model']} (from {result['model_source']})")

# Expected Token Savings: Global Haiku default; enterprise tenants and power users get higher-tier automatically
# Environment: multi-tenant SaaS, B2B platforms, subscription-gated features, enterprise customization
```

---

## Comparison

| Option | Storage | Update Latency | Rollout Support | Audit Trail | Best For |
|--------|---------|---------------|-----------------|-------------|----------|
| 1. File-Based | JSON file | ~5s (TTL cache) | No | No | Dev environments, single server |
| 2. SQLite-Backed | SQLite DB | Immediate | No | Yes | Production with audit requirements |
| 3. Env Variable + Hash | Environment | Restart | Yes (% hash) | No | Containerized, 12-factor apps |
| 4. In-Memory + HTTP | RAM | Immediate | No | No | Low-latency, ops dashboard control |
| 5. Tool Flag Registry | Any | Per-request | No | No | Tool access by subscription tier |
| 6. Hierarchical | Any | Per-request | No | Optional | Multi-tenant SaaS, B2B |

**Recommended defaults:**
- **Single-server prod** → Option 2 (SQLite with audit log)
- **Multi-tenant SaaS** → Option 6 (hierarchical)
- **Gradual rollout** → Option 3 (env + hash)
- **Ops dashboard control** → Option 4 (in-memory + HTTP)
- **Tool access control** → Option 5 (flag-gated tools)
