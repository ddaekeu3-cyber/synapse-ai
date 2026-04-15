---
layout: solution
title: "Agent Doesn't Implement Hot Config Reload Without Restart"
category: config
description: "Agents that require a full restart to apply configuration changes lose in-flight sessions, interrupt active users, and create deployment windows. Hot config reload applies new settings at runtime — model parameters, rate limits, system prompts, tool schemas — without dropping a single request."
tags: [config, hot-reload, runtime, operations, zero-downtime, fsnotify, signals, pydantic]
---

# Agent Doesn't Implement Hot Config Reload Without Restart

## Problem

Operators change agent configuration constantly: adjusting the system prompt to fix a tone issue, increasing rate limits during a traffic spike, swapping the model tier for cost control, enabling a new tool. Every change that requires a restart drops in-flight conversations, causes brief downtime, and creates a ritual of "restart and pray." Hot config reload makes configuration a live knob that takes effect within seconds — with no user impact.

**Symptoms:**
- Changing the system prompt requires restarting the server
- Model changes during traffic spikes require downtime
- Rate limit adjustments cannot take effect immediately
- Operators restart the agent to enable/disable tools
- Config drift between instances because restart timing varies

---

## Option 1: File Watcher with Atomic Config Swap

```python
import anthropic
import json
import threading
import time
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

@dataclass
class AgentConfig:
    model: str = "claude-haiku-4-5-20251001"
    max_tokens: int = 1024
    system_prompt: str = "You are a helpful assistant."
    temperature: float = 1.0
    enabled_tools: list[str] = field(default_factory=list)
    rate_limit_rpm: int = 60
    version: str = "1.0"

    @classmethod
    def from_file(cls, path: str) -> "AgentConfig":
        with open(path) as f:
            data = json.load(f)
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

    def checksum(self) -> str:
        content = json.dumps(self.__dict__, sort_keys=True)
        return hashlib.md5(content.encode()).hexdigest()[:8]

class ConfigWatcher:
    """Watch a config file and reload on change."""

    def __init__(self, config_path: str, poll_interval_s: float = 1.0):
        self._path = Path(config_path)
        self._interval = poll_interval_s
        self._config: Optional[AgentConfig] = None
        self._lock = threading.RLock()
        self._last_mtime: float = 0.0
        self._reload_callbacks: list = []
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def start(self, initial_config: AgentConfig = None):
        """Start watching. Load from file if exists, else use initial_config."""
        if self._path.exists():
            self._config = AgentConfig.from_file(str(self._path))
            self._last_mtime = self._path.stat().st_mtime
        else:
            self._config = initial_config or AgentConfig()
            self._save_current()  # Write defaults to file

        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        print(f"[ConfigWatcher] Watching {self._path} (v{self._config.version}, {self._config.checksum()})")

    def _save_current(self):
        with open(self._path, "w") as f:
            json.dump(self._config.__dict__, f, indent=2)

    def _poll_loop(self):
        while self._running:
            try:
                if self._path.exists():
                    mtime = self._path.stat().st_mtime
                    if mtime != self._last_mtime:
                        new_config = AgentConfig.from_file(str(self._path))
                        old_config = self._config
                        with self._lock:
                            self._config = new_config
                            self._last_mtime = mtime
                        print(f"[ConfigWatcher] Reloaded: v{new_config.version}, checksum={new_config.checksum()}")
                        for cb in self._reload_callbacks:
                            cb(old_config, new_config)
            except Exception as e:
                print(f"[ConfigWatcher] Error: {e}")
            time.sleep(self._interval)

    def on_reload(self, callback):
        self._reload_callbacks.append(callback)
        return self

    @property
    def config(self) -> AgentConfig:
        with self._lock:
            return self._config

    def stop(self):
        self._running = False

class HotReloadableAgent:
    def __init__(self, watcher: ConfigWatcher):
        self._watcher = watcher
        self._client = anthropic.Anthropic()

        # Register reload callback
        watcher.on_reload(self._on_config_change)

    def _on_config_change(self, old: AgentConfig, new: AgentConfig):
        changes = []
        if old.model != new.model:
            changes.append(f"model: {old.model} -> {new.model}")
        if old.system_prompt != new.system_prompt:
            changes.append(f"system_prompt: updated")
        if old.max_tokens != new.max_tokens:
            changes.append(f"max_tokens: {old.max_tokens} -> {new.max_tokens}")
        if old.rate_limit_rpm != new.rate_limit_rpm:
            changes.append(f"rate_limit_rpm: {old.rate_limit_rpm} -> {new.rate_limit_rpm}")
        print(f"[Agent] Config updated: {', '.join(changes) or 'no effective changes'}")

    def respond(self, user_message: str) -> str:
        cfg = self._watcher.config  # Always reads latest config atomically
        response = self._client.messages.create(
            model=cfg.model,
            max_tokens=cfg.max_tokens,
            system=cfg.system_prompt,
            messages=[{"role": "user", "content": user_message}]
        )
        return response.content[0].text

# Demo
import tempfile, os

with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
    config_path = f.name

watcher = ConfigWatcher(config_path, poll_interval_s=0.5)
watcher.start(AgentConfig(model="claude-haiku-4-5-20251001", system_prompt="You are a concise assistant."))
agent = HotReloadableAgent(watcher)

print("\n[Demo] Sending request with initial config...")
r1 = agent.respond("What is Python?")
print(f"Response: {r1[:80]}...\n")

# Simulate operator updating config file
print("[Demo] Operator updates config file (new system prompt)...")
with open(config_path, "w") as f:
    json.dump({
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 512,
        "system_prompt": "You are a formal technical assistant. Use precise language.",
        "temperature": 1.0,
        "enabled_tools": [],
        "rate_limit_rpm": 60,
        "version": "1.1"
    }, f)

time.sleep(1.0)  # Wait for watcher to detect change

print("\n[Demo] Sending request with new config (no restart)...")
r2 = agent.respond("What is Python?")
print(f"Response: {r2[:80]}...")

watcher.stop()
os.unlink(config_path)

# Expected Token Savings: ~0% — config reload adds no token overhead; eliminates downtime cost
# Environment: Production: use inotify (Linux) or FSEvents (macOS) instead of polling for efficiency
```

---

## Option 2: Signal-Based Reload (SIGHUP Pattern)

```python
import anthropic
import json
import os
import signal
import threading
import time
from dataclasses import dataclass, field
from typing import Any

@dataclass
class LiveConfig:
    model: str = "claude-haiku-4-5-20251001"
    system_prompt: str = "You are a helpful assistant."
    max_tokens: int = 1024
    log_level: str = "INFO"
    feature_flags: dict = field(default_factory=dict)

    @classmethod
    def load(cls, path: str) -> "LiveConfig":
        with open(path) as f:
            d = json.load(f)
        return cls(**{k: v for k, v in d.items() if hasattr(cls, k)})

class SignalReloadableConfig:
    """
    Reload configuration on SIGHUP (Unix convention for 'reload config').
    Safe for multi-threaded use via reader-writer pattern.
    """

    def __init__(self, config_path: str):
        self._path = config_path
        self._config: LiveConfig = None
        self._lock = threading.RLock()
        self._reload_count = 0
        self._reload_callbacks = []

    def load_initial(self):
        try:
            config = LiveConfig.load(self._path)
            with self._lock:
                self._config = config
            print(f"[Config] Initial load: model={config.model}, log={config.log_level}")
        except FileNotFoundError:
            with self._lock:
                self._config = LiveConfig()
            print("[Config] No config file found; using defaults")
        return self

    def install_signal_handler(self):
        """Install SIGHUP handler for hot reload."""
        def _handler(signum, frame):
            # Signal handlers must be async-signal-safe
            # Dispatch reload to a thread to avoid deadlocks
            threading.Thread(target=self._do_reload, daemon=True).start()

        signal.signal(signal.SIGHUP, _handler)
        print(f"[Config] SIGHUP handler installed (send: kill -HUP {os.getpid()})")
        return self

    def _do_reload(self):
        try:
            new_config = LiveConfig.load(self._path)
            old_config = self._config
            with self._lock:
                self._config = new_config
                self._reload_count += 1
            print(f"[Config] Reloaded (#{self._reload_count}): model={new_config.model}")
            for cb in self._reload_callbacks:
                try:
                    cb(old_config, new_config)
                except Exception as e:
                    print(f"[Config] Callback error: {e}")
        except Exception as e:
            print(f"[Config] Reload failed: {e} — keeping previous config")

    def on_reload(self, callback):
        self._reload_callbacks.append(callback)

    @property
    def current(self) -> LiveConfig:
        with self._lock:
            return self._config

    def trigger_reload(self):
        """Manually trigger reload (useful for testing without sending signals)."""
        self._do_reload()

def run_signal_reload_demo():
    import tempfile

    # Create initial config file
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump({
            "model": "claude-haiku-4-5-20251001",
            "system_prompt": "You are a helpful assistant.",
            "max_tokens": 512,
            "log_level": "INFO",
            "feature_flags": {"streaming": False, "debug": False}
        }, f)
        config_path = f.name

    cfg = SignalReloadableConfig(config_path)
    cfg.load_initial()
    cfg.install_signal_handler()

    client = anthropic.Anthropic()

    def on_reload(old, new):
        if old.model != new.model:
            print(f"  [Agent] Model switched: {old.model} -> {new.model}")
        if old.system_prompt != new.system_prompt:
            print(f"  [Agent] System prompt updated")
        if old.feature_flags != new.feature_flags:
            print(f"  [Agent] Feature flags changed: {new.feature_flags}")

    cfg.on_reload(on_reload)

    def serve_request(query: str):
        config = cfg.current
        response = client.messages.create(
            model=config.model,
            max_tokens=config.max_tokens,
            system=config.system_prompt,
            messages=[{"role": "user", "content": query}]
        )
        return response.content[0].text

    print(f"\n[Demo] Request 1 (initial config):")
    r1 = serve_request("What is dependency injection?")
    print(f"Response: {r1[:80]}...")

    # Simulate SIGHUP by writing new config and triggering reload
    print(f"\n[Demo] Operator updates config and sends SIGHUP...")
    with open(config_path, "w") as f:
        json.dump({
            "model": "claude-haiku-4-5-20251001",
            "system_prompt": "You are a senior software architect. Be precise and technical.",
            "max_tokens": 768,
            "log_level": "DEBUG",
            "feature_flags": {"streaming": True, "debug": True}
        }, f)

    cfg.trigger_reload()  # Simulates: os.kill(os.getpid(), signal.SIGHUP)
    time.sleep(0.1)

    print(f"\n[Demo] Request 2 (new config, no restart):")
    r2 = serve_request("What is dependency injection?")
    print(f"Response: {r2[:80]}...")
    print(f"\nReload count: {cfg._reload_count}")

    os.unlink(config_path)

run_signal_reload_demo()

# Expected Token Savings: ~0% — SIGHUP reload is instantaneous; zero downtime vs restart
# Environment: Linux/macOS production; Windows uses named pipes or HTTP admin endpoint instead
```

---

## Option 3: HTTP Admin Endpoint for Live Config Updates

```python
import anthropic
import json
import threading
import time
from dataclasses import dataclass, field
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Optional
import urllib.parse

@dataclass
class RuntimeConfig:
    model: str = "claude-haiku-4-5-20251001"
    system_prompt: str = "You are a helpful assistant."
    max_tokens: int = 1024
    rate_limit_rpm: int = 60
    debug_mode: bool = False
    allowed_tools: list[str] = field(default_factory=lambda: ["search", "calculate"])

    def to_dict(self) -> dict:
        return {
            "model": self.model,
            "system_prompt": self.system_prompt[:80] + "..." if len(self.system_prompt) > 80 else self.system_prompt,
            "max_tokens": self.max_tokens,
            "rate_limit_rpm": self.rate_limit_rpm,
            "debug_mode": self.debug_mode,
            "allowed_tools": self.allowed_tools
        }

class ConfigStore:
    def __init__(self):
        self._config = RuntimeConfig()
        self._lock = threading.RLock()
        self._history: list[dict] = []
        self._version: int = 1

    def get(self) -> RuntimeConfig:
        with self._lock:
            return self._config

    def update(self, updates: dict) -> tuple[RuntimeConfig, list[str]]:
        with self._lock:
            old = self._config
            changed = []
            new_dict = old.__dict__.copy()

            for key, value in updates.items():
                if hasattr(old, key) and getattr(old, key) != value:
                    new_dict[key] = value
                    changed.append(f"{key}: {getattr(old, key)!r} -> {value!r}")

            if changed:
                self._config = RuntimeConfig(**new_dict)
                self._version += 1
                self._history.append({
                    "version": self._version,
                    "changes": changed,
                    "timestamp": time.time()
                })

            return self._config, changed

    def history(self, limit: int = 10) -> list[dict]:
        with self._lock:
            return self._history[-limit:]

    @property
    def version(self) -> int:
        with self._lock:
            return self._version

store = ConfigStore()

class AdminHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # Suppress default HTTP logging

    def do_GET(self):
        if self.path == "/config":
            cfg = store.get()
            body = json.dumps({"version": store.version, "config": cfg.to_dict()}, indent=2).encode()
            self._respond(200, body)
        elif self.path == "/config/history":
            body = json.dumps(store.history()).encode()
            self._respond(200, body)
        else:
            self._respond(404, b'{"error": "Not found"}')

    def do_POST(self):
        if self.path == "/config":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                updates = json.loads(body)
                new_cfg, changed = store.update(updates)
                response = {
                    "version": store.version,
                    "changed": changed,
                    "config": new_cfg.to_dict()
                }
                print(f"[Admin] Config updated v{store.version}: {changed}")
                self._respond(200, json.dumps(response).encode())
            except Exception as e:
                self._respond(400, json.dumps({"error": str(e)}).encode())
        else:
            self._respond(404, b'{"error": "Not found"}')

    def _respond(self, code: int, body: bytes):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

def start_admin_server(port: int = 8765):
    server = HTTPServer(("localhost", port), AdminHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"[Admin] Config endpoint: http://localhost:{port}/config")
    return server

class HttpConfigurableAgent:
    def __init__(self, config_store: ConfigStore):
        self._store = config_store
        self._client = anthropic.Anthropic()

    def respond(self, user_message: str) -> str:
        cfg = self._store.get()  # Always reads current config
        tools = [
            {
                "name": t,
                "description": f"Tool: {t}",
                "input_schema": {"type": "object", "properties": {"input": {"type": "string"}}, "required": ["input"]}
            }
            for t in cfg.allowed_tools
        ]
        response = self._client.messages.create(
            model=cfg.model,
            max_tokens=cfg.max_tokens,
            system=cfg.system_prompt,
            tools=tools if tools else anthropic.NOT_GIVEN,
            messages=[{"role": "user", "content": user_message}]
        )
        return response.content[0].text if response.content[0].type == "text" else "[tool use]"

def run_http_reload_demo():
    import urllib.request

    server = start_admin_server(8765)
    agent = HttpConfigurableAgent(store)

    print("\n[Demo] Initial config state:")
    r = urllib.request.urlopen("http://localhost:8765/config")
    print(json.dumps(json.loads(r.read()), indent=2))

    # Agent handles a request with initial config
    answer = agent.respond("What is a REST API?")
    print(f"\n[Request 1] {answer[:80]}...")

    # Operator updates config via HTTP (no restart)
    print("\n[Demo] Operator updates system prompt and model via HTTP PATCH...")
    payload = json.dumps({
        "system_prompt": "You are a terse technical assistant. One paragraph maximum.",
        "max_tokens": 256,
        "debug_mode": True
    }).encode()
    req = urllib.request.Request(
        "http://localhost:8765/config",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    r = urllib.request.urlopen(req)
    result = json.loads(r.read())
    print(f"Updated: {result['changed']}")

    # Same agent serves next request with new config
    answer2 = agent.respond("What is a REST API?")
    print(f"\n[Request 2] {answer2[:80]}...")

    r = urllib.request.urlopen("http://localhost:8765/config/history")
    history = json.loads(r.read())
    print(f"\nConfig history: {[h['changes'] for h in history]}")

    server.shutdown()

run_http_reload_demo()

# Expected Token Savings: ~0% — HTTP admin adds no token overhead; enables live tuning without restart
# Environment: Kubernetes: expose admin endpoint as internal ClusterIP service; use RBAC to restrict access
```

---

## Option 4: Environment Variable Hot Reload with Namespace Isolation

```python
import anthropic
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class EnvConfig:
    """Configuration loaded from environment variables with namespace prefix."""
    model: str = "claude-haiku-4-5-20251001"
    max_tokens: int = 1024
    system_prompt: str = "You are a helpful assistant."
    rate_limit_rpm: int = 60
    timeout_s: float = 30.0
    log_requests: bool = False

    ENV_PREFIX = "AGENT_"
    TYPE_MAP = {
        "model": str, "max_tokens": int, "system_prompt": str,
        "rate_limit_rpm": int, "timeout_s": float, "log_requests": bool
    }

    @classmethod
    def from_env(cls) -> "EnvConfig":
        kwargs = {}
        for field_name, field_type in cls.TYPE_MAP.items():
            env_key = cls.ENV_PREFIX + field_name.upper()
            val = os.environ.get(env_key)
            if val is not None:
                if field_type == bool:
                    kwargs[field_name] = val.lower() in ("1", "true", "yes")
                else:
                    kwargs[field_name] = field_type(val)
        return cls(**kwargs)

    def diff(self, other: "EnvConfig") -> list[str]:
        changes = []
        for f in self.TYPE_MAP:
            old_val = getattr(self, f)
            new_val = getattr(other, f)
            if old_val != new_val:
                changes.append(f"{f}: {old_val!r} -> {new_val!r}")
        return changes

class EnvConfigWatcher:
    """Poll environment variables for changes (useful in containerized environments)."""

    def __init__(self, poll_interval_s: float = 2.0):
        self._config: Optional[EnvConfig] = None
        self._lock = threading.RLock()
        self._interval = poll_interval_s
        self._callbacks = []
        self._running = False

    def start(self):
        self._config = EnvConfig.from_env()
        self._running = True
        threading.Thread(target=self._poll, daemon=True).start()
        print(f"[EnvConfig] Watching {EnvConfig.ENV_PREFIX}* variables")
        return self

    def _poll(self):
        while self._running:
            time.sleep(self._interval)
            try:
                new = EnvConfig.from_env()
                changes = self._config.diff(new)
                if changes:
                    old = self._config
                    with self._lock:
                        self._config = new
                    print(f"[EnvConfig] Reload: {changes}")
                    for cb in self._callbacks:
                        cb(old, new)
            except Exception as e:
                print(f"[EnvConfig] Poll error: {e}")

    def on_change(self, callback):
        self._callbacks.append(callback)

    @property
    def current(self) -> EnvConfig:
        with self._lock:
            return self._config

    def stop(self):
        self._running = False

def run_env_reload_demo():
    client = anthropic.Anthropic()

    # Set initial environment
    os.environ["AGENT_MODEL"] = "claude-haiku-4-5-20251001"
    os.environ["AGENT_MAX_TOKENS"] = "512"
    os.environ["AGENT_SYSTEM_PROMPT"] = "You are concise."
    os.environ["AGENT_LOG_REQUESTS"] = "false"

    watcher = EnvConfigWatcher(poll_interval_s=0.5).start()
    watcher.on_change(lambda old, new: print(f"  [Agent] Applying env changes..."))

    def serve(query: str) -> str:
        cfg = watcher.current
        if cfg.log_requests:
            print(f"  [Log] Request: {query!r}")
        response = client.messages.create(
            model=cfg.model,
            max_tokens=cfg.max_tokens,
            system=cfg.system_prompt,
            messages=[{"role": "user", "content": query}]
        )
        return response.content[0].text

    print("\n[Request 1] Initial config:")
    r1 = serve("What is a hash map?")
    print(f"Response: {r1[:80]}...")

    # Simulate Kubernetes configmap update / ECS env var change
    print("\n[Simulating env var update (kubectl set env / ECS task update)]...")
    os.environ["AGENT_MAX_TOKENS"] = "256"
    os.environ["AGENT_SYSTEM_PROMPT"] = "You are a senior engineer. Be precise."
    os.environ["AGENT_LOG_REQUESTS"] = "true"
    time.sleep(1.0)  # Wait for watcher to detect

    print("\n[Request 2] New config (no restart):")
    r2 = serve("What is a hash map?")
    print(f"Response: {r2[:80]}...")

    watcher.stop()

run_env_reload_demo()

# Expected Token Savings: ~0% — env var polling adds 2ms overhead; ideal for Kubernetes/ECS deployments
# Environment: Containerized deployments where config is injected via env vars from ConfigMaps/SSM
```

---

## Option 5: Versioned Config with Rollback Support

```python
import anthropic
import json
import threading
import time
from dataclasses import dataclass, field
from typing import Optional
from collections import deque

@dataclass
class VersionedConfig:
    version: int
    config: dict
    applied_at: float = field(default_factory=time.time)
    applied_by: str = "system"
    rollback_reason: str = ""

class VersionedConfigManager:
    """
    Hot-reload config with full version history and one-click rollback.
    Validates new config before applying — rejects invalid changes.
    """

    REQUIRED_FIELDS = {"model", "max_tokens", "system_prompt"}
    VALID_MODELS = {
        "claude-haiku-4-5-20251001",
        "claude-sonnet-4-6",
        "claude-opus-4-6"
    }

    def __init__(self, max_history: int = 10):
        self._history: deque[VersionedConfig] = deque(maxlen=max_history)
        self._current: Optional[VersionedConfig] = None
        self._lock = threading.RLock()
        self._version_counter = 0
        self._callbacks = []

    def initialize(self, config: dict, applied_by: str = "init"):
        self._apply(config, applied_by)

    def _validate(self, config: dict) -> list[str]:
        errors = []
        for field in self.REQUIRED_FIELDS:
            if field not in config:
                errors.append(f"Missing required field: {field}")
        if "model" in config and config["model"] not in self.VALID_MODELS:
            errors.append(f"Invalid model: {config['model']}. Must be one of {self.VALID_MODELS}")
        if "max_tokens" in config:
            if not isinstance(config["max_tokens"], int) or config["max_tokens"] < 1:
                errors.append("max_tokens must be a positive integer")
        if "rate_limit_rpm" in config and config.get("rate_limit_rpm", 1) < 1:
            errors.append("rate_limit_rpm must be >= 1")
        return errors

    def _apply(self, config: dict, applied_by: str) -> VersionedConfig:
        with self._lock:
            self._version_counter += 1
            versioned = VersionedConfig(
                version=self._version_counter,
                config=config.copy(),
                applied_by=applied_by
            )
            if self._current:
                self._history.append(self._current)
            self._current = versioned
            for cb in self._callbacks:
                cb(versioned)
            return versioned

    def apply(self, config: dict, applied_by: str = "operator") -> tuple[bool, str, Optional[VersionedConfig]]:
        """Apply new config with validation. Returns (success, message, versioned_config)."""
        errors = self._validate(config)
        if errors:
            return False, f"Validation failed: {'; '.join(errors)}", None

        versioned = self._apply(config, applied_by)
        print(f"[Config v{versioned.version}] Applied by {applied_by}")
        return True, f"Applied as v{versioned.version}", versioned

    def rollback(self, steps: int = 1) -> tuple[bool, str]:
        with self._lock:
            if len(self._history) < steps:
                return False, f"Cannot roll back {steps} step(s); only {len(self._history)} in history"
            target = list(self._history)[-steps]
            target_config = target.config.copy()

        success, msg, versioned = self.apply(
            target_config,
            applied_by=f"rollback_to_v{target.version}"
        )
        if success:
            versioned.rollback_reason = f"Rolled back to v{target.version}"
        return success, msg

    @property
    def current(self) -> dict:
        with self._lock:
            return self._current.config if self._current else {}

    @property
    def current_version(self) -> int:
        with self._lock:
            return self._current.version if self._current else 0

    def on_apply(self, callback):
        self._callbacks.append(callback)

    def history_summary(self) -> list[dict]:
        with self._lock:
            return [
                {"version": v.version, "by": v.applied_by,
                 "model": v.config.get("model"), "rollback": v.rollback_reason}
                for v in self._history
            ]

def run_versioned_config_demo():
    client = anthropic.Anthropic()
    mgr = VersionedConfigManager(max_history=5)

    mgr.on_apply(lambda v: print(f"  [Hook] Config v{v.version} now active"))

    # Initialize
    mgr.initialize({
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 512,
        "system_prompt": "You are a helpful assistant."
    })

    def serve(query: str) -> str:
        cfg = mgr.current
        response = client.messages.create(
            model=cfg["model"],
            max_tokens=cfg["max_tokens"],
            system=cfg["system_prompt"],
            messages=[{"role": "user", "content": query}]
        )
        return response.content[0].text

    print(f"\n[v{mgr.current_version}] Request:")
    print(serve("What is Python?")[:80] + "...")

    # Apply new config
    ok, msg, _ = mgr.apply({
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 256,
        "system_prompt": "You are extremely terse. One sentence only."
    }, applied_by="ops_team")
    print(f"\nApply result: {msg}")

    print(f"\n[v{mgr.current_version}] Request:")
    print(serve("What is Python?")[:80] + "...")

    # Try invalid config — rejected
    ok, msg, _ = mgr.apply({
        "model": "gpt-4",  # Invalid model
        "max_tokens": -5,   # Invalid value
        "system_prompt": "..."
    })
    print(f"\nInvalid config: {msg}")

    # Rollback
    ok, msg = mgr.rollback(steps=1)
    print(f"\nRollback: {msg}")
    print(f"\n[v{mgr.current_version}] Request after rollback:")
    print(serve("What is Python?")[:80] + "...")
    print(f"\nHistory: {mgr.history_summary()}")

run_versioned_config_demo()

# Expected Token Savings: ~0% — versioning adds no overhead; rollback prevents bad config incidents
# Environment: Production: store history in PostgreSQL; emit version changes to audit log
```

---

## Option 6: Multi-Agent Config Broadcast via Pub/Sub

```python
import anthropic
import json
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field

@dataclass
class ConfigMessage:
    message_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    config: dict = field(default_factory=dict)
    source: str = "operator"
    timestamp: float = field(default_factory=time.time)

class ConfigBus:
    """In-process pub/sub config bus. Replace with Redis pub/sub or Kafka for distributed agents."""

    def __init__(self):
        self._subscribers: dict[str, queue.Queue] = {}
        self._lock = threading.Lock()

    def subscribe(self, subscriber_id: str) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=10)
        with self._lock:
            self._subscribers[subscriber_id] = q
        return q

    def publish(self, message: ConfigMessage):
        with self._lock:
            subs = dict(self._subscribers)
        delivered = 0
        for sid, q in subs.items():
            try:
                q.put_nowait(message)
                delivered += 1
            except queue.Full:
                print(f"[Bus] Queue full for {sid}")
        print(f"[Bus] Published config update to {delivered} agents: {message.config}")

class BroadcastAgent:
    def __init__(self, agent_id: str, initial_config: dict, bus: ConfigBus):
        self.agent_id = agent_id
        self._config = initial_config.copy()
        self._lock = threading.RLock()
        self._client = anthropic.Anthropic()
        self._queue = bus.subscribe(agent_id)
        self._running = True

        # Start listener thread
        threading.Thread(target=self._listen_for_updates, daemon=True).start()

    def _listen_for_updates(self):
        while self._running:
            try:
                msg: ConfigMessage = self._queue.get(timeout=0.1)
                old_model = self._config.get("model")
                with self._lock:
                    self._config.update(msg.config)
                new_model = self._config.get("model")
                print(f"  [{self.agent_id}] Config updated from {msg.source} "
                      f"(model: {old_model} -> {new_model})")
            except queue.Empty:
                continue

    def respond(self, query: str) -> str:
        with self._lock:
            cfg = self._config.copy()
        response = self._client.messages.create(
            model=cfg.get("model", "claude-haiku-4-5-20251001"),
            max_tokens=cfg.get("max_tokens", 256),
            system=cfg.get("system_prompt", "You are a helpful assistant."),
            messages=[{"role": "user", "content": query}]
        )
        return response.content[0].text

    def stop(self):
        self._running = False

def run_broadcast_demo():
    bus = ConfigBus()

    initial_config = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 256,
        "system_prompt": "You are a helpful assistant."
    }

    # Spawn 3 agent instances sharing the same config bus
    agents = [
        BroadcastAgent(f"agent_{i}", initial_config, bus)
        for i in range(3)
    ]

    print("Running 3 agent instances with shared config bus\n")

    # All agents serve requests with initial config
    for agent in agents:
        r = agent.respond("What is Python?")
        print(f"[{agent.agent_id}] {r[:60]}...")

    time.sleep(0.1)

    # Operator pushes config update — all agents receive it simultaneously
    print(f"\n[Operator] Broadcasting config update to all agents...")
    bus.publish(ConfigMessage(
        config={
            "max_tokens": 128,
            "system_prompt": "You are an ultra-terse assistant. One sentence maximum.",
        },
        source="ops_dashboard"
    ))
    time.sleep(0.3)  # Allow broadcast to propagate

    print(f"\nAll agents now using new config:")
    for agent in agents:
        r = agent.respond("What is Python?")
        print(f"[{agent.agent_id}] {r[:60]}...")

    for agent in agents:
        agent.stop()

run_broadcast_demo()

# Expected Token Savings: ~0% overhead; broadcast eliminates config drift between agent instances
# Environment: Replace ConfigBus with Redis pub/sub (redis.asyncio) for cross-process broadcasting
```

---

## Comparison

| Option | Trigger | Persistence | Rollback | Multi-Instance | Best For |
|--------|---------|-------------|---------|---------------|----------|
| File Watcher | File change | File | Manual | No | Simple single-process agents |
| SIGHUP Signal | OS signal | File | Manual | No | Unix services following 12-factor |
| HTTP Admin | POST request | Memory | Via history API | No | Kubernetes operators and dashboards |
| Env Var Watcher | Env change | OS env | kubectl rollout | Yes (per pod) | Containerized ECS/K8s deployments |
| Versioned Config | API call | Memory + optional DB | One command | No | Production with audit requirements |
| Config Bus | Publish | Memory | Via re-publish | Yes (all instances) | Multi-instance agents sharing config |

**Recommendation:** Use **Option 1** (file watcher) for single-process agents — it's the simplest and requires no infrastructure. Use **Option 3** (HTTP admin endpoint) for Kubernetes deployments where you want operator tooling. Use **Option 6** (config bus) when running multiple agent instances that must stay in sync — a single `publish()` call updates all of them atomically.
