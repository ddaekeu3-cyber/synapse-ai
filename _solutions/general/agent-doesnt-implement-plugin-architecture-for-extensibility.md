---
layout: solution
title: "Agent Doesn't Implement Plugin Architecture for Extensibility"
description: "How to build a plugin system that lets teams add new tools, capabilities, and behaviors to an agent at runtime without modifying core agent code."
tags: [general, architecture, plugins, extensibility, tools, runtime]
difficulty: intermediate
solution_count: 6
---

## Problem

Every new tool, integration, or capability requires modifying the core agent code, redeploying, and retesting the entire system. Teams competing to add features introduce merge conflicts, break shared abstractions, and can't ship independently. The agent becomes a monolith that's hard to extend and impossible to customize per-tenant.

```python
# Bad: hardcoded tool list — adding any tool requires editing core agent code
TOOLS = {
    "search": search_tool,
    "calculator": calculator_tool,
    "email": email_tool,  # adding this required a PR to core agent
}
# Every new integration = touch core, redeploy, retest everything
```

---

## Solution 1 — Simple Registry-Based Plugin System

Plugins register themselves into a central registry at import time. The agent loads all registered plugins without knowing about them individually.

```python
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

@dataclass
class ToolPlugin:
    name: str
    description: str
    input_schema: dict
    handler: Callable[..., Awaitable[Any]]
    tags: list[str] = field(default_factory=list)

class PluginRegistry:
    _instance: "PluginRegistry | None" = None

    def __init__(self):
        self._plugins: dict[str, ToolPlugin] = {}

    @classmethod
    def get(cls) -> "PluginRegistry":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def register(self, plugin: ToolPlugin) -> None:
        if plugin.name in self._plugins:
            raise ValueError(f"Plugin '{plugin.name}' already registered")
        self._plugins[plugin.name] = plugin
        print(f"Plugin registered: {plugin.name}")

    def tool(self, name: str, description: str, schema: dict, tags: list[str] = None):
        """Decorator for registering a function as a tool plugin."""
        def decorator(fn: Callable) -> Callable:
            self.register(ToolPlugin(
                name=name,
                description=description,
                input_schema=schema,
                handler=fn,
                tags=tags or [],
            ))
            return fn
        return decorator

    def get_plugin(self, name: str) -> ToolPlugin | None:
        return self._plugins.get(name)

    def list_tools(self, tags: list[str] = None) -> list[ToolPlugin]:
        plugins = list(self._plugins.values())
        if tags:
            plugins = [p for p in plugins if any(t in p.tags for t in tags)]
        return plugins

    def anthropic_tool_schemas(self) -> list[dict]:
        """Convert registered plugins to Anthropic tool_choice format."""
        return [
            {
                "name": p.name,
                "description": p.description,
                "input_schema": p.input_schema,
            }
            for p in self._plugins.values()
        ]

registry = PluginRegistry.get()

# --- plugins/search.py (independent module, no core changes needed) ---
@registry.tool(
    name="web_search",
    description="Search the web for current information",
    schema={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    tags=["research", "web"],
)
async def web_search(query: str) -> str:
    return f"Search results for: {query}"

# --- plugins/calculator.py ---
@registry.tool(
    name="calculate",
    description="Evaluate a mathematical expression",
    schema={"type": "object", "properties": {"expression": {"type": "string"}}, "required": ["expression"]},
    tags=["math"],
)
async def calculate(expression: str) -> float:
    return eval(expression, {"__builtins__": {}})

# Agent uses registry without knowing about specific plugins
async def run_agent(message: str) -> str:
    from anthropic import AsyncAnthropic
    client = AsyncAnthropic()

    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        tools=registry.anthropic_tool_schemas(),
        messages=[{"role": "user", "content": message}],
    )

    if response.stop_reason == "tool_use":
        tool_use = next(b for b in response.content if b.type == "tool_use")
        plugin = registry.get_plugin(tool_use.name)
        if plugin:
            return str(await plugin.handler(**tool_use.input))
    return response.content[0].text
```

---

## Solution 2 — File-System Plugin Discovery (Hot Reload)

Scan a `plugins/` directory at startup (and on demand) to discover and load plugin modules dynamically. Teams drop a `.py` file into the directory to add a new capability.

```python
import importlib
import importlib.util
import sys
from pathlib import Path
from typing import Any

PLUGINS_DIR = Path("plugins")

class FileSystemPluginLoader:
    def __init__(self, registry: PluginRegistry, plugins_dir: Path = PLUGINS_DIR):
        self._registry = registry
        self._dir = plugins_dir
        self._loaded: dict[str, float] = {}  # module_name -> mtime

    def discover_and_load(self) -> list[str]:
        """Load all .py files in the plugins directory. Returns newly loaded module names."""
        if not self._dir.exists():
            return []

        loaded = []
        for path in self._dir.glob("*.py"):
            if path.name.startswith("_"):
                continue
            mtime = path.stat().st_mtime
            module_name = f"plugins.{path.stem}"

            # Skip if already loaded with same mtime
            if self._loaded.get(module_name) == mtime:
                continue

            try:
                spec = importlib.util.spec_from_file_location(module_name, path)
                module = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = module
                spec.loader.exec_module(module)
                self._loaded[module_name] = mtime
                loaded.append(module_name)
                print(f"Loaded plugin module: {path.name}")
            except Exception as e:
                print(f"Failed to load plugin {path.name}: {e}")

        return loaded

    def hot_reload(self) -> list[str]:
        """Reload changed plugin files without restarting the agent."""
        changed = []
        for path in self._dir.glob("*.py"):
            if path.name.startswith("_"):
                continue
            mtime = path.stat().st_mtime
            module_name = f"plugins.{path.stem}"
            if self._loaded.get(module_name, 0) != mtime:
                changed.append(module_name)

        if changed:
            # Unregister plugins from changed modules (simplified)
            print(f"Hot-reloading {len(changed)} changed plugin modules")
            return self.discover_and_load()
        return []

# Startup
loader = FileSystemPluginLoader(registry)
loader.discover_and_load()

# Usage: drop a new .py file into plugins/ — no restart needed
# plugins/
#   weather.py   <- new file, discovered on next hot_reload()
#   search.py
#   calculator.py

# In your web server: reload on each request (dev) or on SIGHUP (prod)
import signal
def on_sighup(sig, frame):
    reloaded = loader.hot_reload()
    print(f"Hot-reloaded: {reloaded}")
signal.signal(signal.SIGHUP, on_sighup)
```

---

## Solution 3 — Versioned Plugin Contracts with Compatibility Checking

Enforce a plugin API version contract so plugin authors know exactly what interface to implement and the registry rejects incompatible plugins.

```python
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable
import inspect

PLUGIN_API_VERSION = "2.0"

@runtime_checkable
class AgentPlugin(Protocol):
    """Contract that all plugins must satisfy."""
    plugin_name: str
    plugin_version: str
    api_version: str
    tool_schema: dict

    async def execute(self, **kwargs: Any) -> Any: ...
    async def health_check(self) -> bool: ...

@dataclass
class PluginCompatibilityError(Exception):
    plugin_name: str
    reason: str

def check_compatibility(plugin: Any, required_api: str = PLUGIN_API_VERSION) -> None:
    # Check it satisfies the Protocol
    if not isinstance(plugin, AgentPlugin):
        missing = []
        for attr in ["plugin_name", "plugin_version", "api_version", "tool_schema"]:
            if not hasattr(plugin, attr):
                missing.append(attr)
        if not hasattr(plugin, "execute") or not hasattr(plugin, "health_check"):
            missing.extend(["execute", "health_check"])
        raise PluginCompatibilityError(
            plugin_name=getattr(plugin, "plugin_name", str(type(plugin))),
            reason=f"Missing required attributes: {missing}",
        )

    # Semver major version must match
    plugin_major = plugin.api_version.split(".")[0]
    required_major = required_api.split(".")[0]
    if plugin_major != required_major:
        raise PluginCompatibilityError(
            plugin_name=plugin.plugin_name,
            reason=f"API version mismatch: plugin={plugin.api_version} required={required_api}",
        )

    # execute() must be async
    if not inspect.iscoroutinefunction(plugin.execute):
        raise PluginCompatibilityError(
            plugin_name=plugin.plugin_name,
            reason="execute() must be an async method",
        )

class VersionedPluginRegistry:
    def __init__(self):
        self._plugins: dict[str, AgentPlugin] = {}

    def register(self, plugin: Any) -> None:
        check_compatibility(plugin)
        self._plugins[plugin.plugin_name] = plugin
        print(f"Registered: {plugin.plugin_name} v{plugin.plugin_version} (API {plugin.api_version})")

    async def execute(self, name: str, **kwargs: Any) -> Any:
        plugin = self._plugins.get(name)
        if not plugin:
            raise ValueError(f"No plugin named '{name}'")
        return await plugin.execute(**kwargs)

# Example plugin implementing the contract
class WeatherPlugin:
    plugin_name = "get_weather"
    plugin_version = "1.3.0"
    api_version = "2.0"
    tool_schema = {
        "type": "object",
        "properties": {"location": {"type": "string"}},
        "required": ["location"],
    }

    async def execute(self, location: str) -> dict:
        return {"location": location, "temp": 22, "condition": "sunny"}

    async def health_check(self) -> bool:
        return True

vregistry = VersionedPluginRegistry()
vregistry.register(WeatherPlugin())
```

---

## Solution 4 — Per-Tenant Plugin Sets with Capability Scoping

Load different plugin sets per tenant/user role. One tenant gets `[web_search, calculator]`; another gets `[internal_db, crm_lookup]`. The core agent is identical.

```python
from dataclasses import dataclass, field
from typing import Any

@dataclass
class TenantConfig:
    tenant_id: str
    enabled_plugins: list[str]
    plugin_overrides: dict[str, dict] = field(default_factory=dict)
    max_tool_calls: int = 20

class TenantPluginRouter:
    def __init__(self, global_registry: PluginRegistry):
        self._global = global_registry
        self._tenant_configs: dict[str, TenantConfig] = {}

    def configure_tenant(self, config: TenantConfig) -> None:
        self._tenant_configs[config.tenant_id] = config

    def get_tools_for_tenant(self, tenant_id: str) -> list[ToolPlugin]:
        config = self._tenant_configs.get(tenant_id)
        if not config:
            return []  # no tools if tenant not configured

        plugins = []
        for name in config.enabled_plugins:
            plugin = self._global.get_plugin(name)
            if plugin:
                plugins.append(plugin)
        return plugins

    def get_schema_for_tenant(self, tenant_id: str) -> list[dict]:
        plugins = self.get_tools_for_tenant(tenant_id)
        config = self._tenant_configs.get(tenant_id, TenantConfig(tenant_id, []))

        schemas = []
        for p in plugins:
            schema = {"name": p.name, "description": p.description,
                      "input_schema": p.input_schema}
            # Apply per-tenant overrides (e.g., restrict search to specific domains)
            overrides = config.plugin_overrides.get(p.name, {})
            schema.update(overrides)
            schemas.append(schema)
        return schemas

    async def execute_for_tenant(self, tenant_id: str, tool_name: str,
                                  args: dict) -> Any:
        config = self._tenant_configs.get(tenant_id)
        if not config or tool_name not in config.enabled_plugins:
            raise PermissionError(
                f"Tenant '{tenant_id}' does not have access to tool '{tool_name}'"
            )
        plugin = self._global.get_plugin(tool_name)
        if not plugin:
            raise ValueError(f"Unknown tool: {tool_name}")
        return await plugin.handler(**args)

router = TenantPluginRouter(registry)
router.configure_tenant(TenantConfig(
    tenant_id="enterprise-corp",
    enabled_plugins=["web_search", "calculate"],
    max_tool_calls=50,
))
router.configure_tenant(TenantConfig(
    tenant_id="free-tier",
    enabled_plugins=["calculate"],  # limited access
    max_tool_calls=5,
))
```

---

## Solution 5 — Async Plugin Lifecycle with Init, Teardown, and Health

Plugins with slow initialization (DB connections, model loads) get proper async `setup()` / `teardown()` lifecycle hooks, and the registry health-checks them before routing calls.

```python
import asyncio
from abc import ABC, abstractmethod
from typing import Any

class LifecyclePlugin(ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    async def setup(self) -> None:
        """Called once at startup. Load models, open connections, etc."""

    @abstractmethod
    async def teardown(self) -> None:
        """Called at shutdown. Close connections, flush buffers."""

    @abstractmethod
    async def health_check(self) -> bool:
        """Returns True if plugin is ready to handle requests."""

    @abstractmethod
    async def execute(self, **kwargs) -> Any:
        """Handle a tool call."""

class LifecyclePluginRegistry:
    def __init__(self):
        self._plugins: dict[str, LifecyclePlugin] = {}
        self._healthy: dict[str, bool] = {}

    def add(self, plugin: LifecyclePlugin) -> None:
        self._plugins[plugin.name] = plugin

    async def startup(self) -> None:
        """Initialize all plugins concurrently."""
        async with asyncio.TaskGroup() as tg:
            for plugin in self._plugins.values():
                tg.create_task(self._init_one(plugin))

    async def _init_one(self, plugin: LifecyclePlugin) -> None:
        try:
            await plugin.setup()
            self._healthy[plugin.name] = True
            print(f"Plugin '{plugin.name}' initialized")
        except Exception as e:
            self._healthy[plugin.name] = False
            print(f"Plugin '{plugin.name}' failed to initialize: {e}")

    async def shutdown(self) -> None:
        async with asyncio.TaskGroup() as tg:
            for plugin in self._plugins.values():
                tg.create_task(plugin.teardown())

    async def health_check_all(self) -> dict[str, bool]:
        results = await asyncio.gather(
            *[plugin.health_check() for plugin in self._plugins.values()],
            return_exceptions=True,
        )
        for plugin, result in zip(self._plugins.values(), results):
            self._healthy[plugin.name] = bool(result) and not isinstance(result, Exception)
        return dict(self._healthy)

    async def execute(self, tool_name: str, **kwargs) -> Any:
        plugin = self._plugins.get(tool_name)
        if not plugin:
            raise ValueError(f"Plugin '{tool_name}' not found")
        if not self._healthy.get(tool_name, False):
            raise RuntimeError(f"Plugin '{tool_name}' is unhealthy — call rejected")
        return await plugin.execute(**kwargs)

# Example plugin
class EmbeddingPlugin(LifecyclePlugin):
    name = "embed_text"
    _model = None

    async def setup(self) -> None:
        await asyncio.sleep(0.5)  # simulate model loading
        self._model = "loaded_model"

    async def teardown(self) -> None:
        self._model = None

    async def health_check(self) -> bool:
        return self._model is not None

    async def execute(self, text: str) -> list[float]:
        return [0.1, 0.2, 0.3]  # mock embedding

lregistry = LifecyclePluginRegistry()
lregistry.add(EmbeddingPlugin())

async def main():
    await lregistry.startup()
    health = await lregistry.health_check_all()
    print(f"Health: {health}")
    result = await lregistry.execute("embed_text", text="hello world")
    await lregistry.shutdown()

asyncio.run(main())
```

---

## Solution 6 — Remote Plugin Loading via HTTP Endpoint

Load plugin schemas and handlers from remote HTTP endpoints. Teams deploy their own plugin services and register the URL; the agent proxies calls to them.

```python
import asyncio
import httpx
from dataclasses import dataclass
from typing import Any

@dataclass
class RemotePlugin:
    name: str
    description: str
    input_schema: dict
    endpoint_url: str    # POST {endpoint_url}/execute
    health_url: str      # GET  {endpoint_url}/health
    timeout: float = 10.0

class RemotePluginRegistry:
    def __init__(self):
        self._plugins: dict[str, RemotePlugin] = {}
        self._client = httpx.AsyncClient()

    def register_remote(self, plugin: RemotePlugin) -> None:
        self._plugins[plugin.name] = plugin
        print(f"Remote plugin registered: {plugin.name} -> {plugin.endpoint_url}")

    async def execute(self, name: str, **kwargs: Any) -> Any:
        plugin = self._plugins.get(name)
        if not plugin:
            raise ValueError(f"Unknown remote plugin: {name}")

        response = await self._client.post(
            f"{plugin.endpoint_url}/execute",
            json=kwargs,
            timeout=plugin.timeout,
        )
        response.raise_for_status()
        return response.json()

    async def health_check_all(self) -> dict[str, bool]:
        async def check_one(plugin: RemotePlugin) -> tuple[str, bool]:
            try:
                r = await self._client.get(plugin.health_url, timeout=3.0)
                return plugin.name, r.status_code == 200
            except Exception:
                return plugin.name, False

        results = await asyncio.gather(
            *[check_one(p) for p in self._plugins.values()]
        )
        return dict(results)

    def anthropic_schemas(self) -> list[dict]:
        return [
            {"name": p.name, "description": p.description, "input_schema": p.input_schema}
            for p in self._plugins.values()
        ]

    async def aclose(self) -> None:
        await self._client.aclose()

# Team A deploys their plugin service at https://tools.team-a.internal
# Team B deploys at https://tools.team-b.internal
# No changes to core agent needed

rregistry = RemotePluginRegistry()
rregistry.register_remote(RemotePlugin(
    name="team_a_crm_lookup",
    description="Look up customer records in Team A's CRM",
    input_schema={"type": "object", "properties": {"customer_id": {"type": "string"}},
                  "required": ["customer_id"]},
    endpoint_url="https://tools.team-a.internal",
    health_url="https://tools.team-a.internal/health",
))
```

---

## Comparison

| Approach | Zero Core Changes | Hot Reload | Per-Tenant | Lifecycle Mgmt | Cross-Team | Best For |
|---|---|---|---|---|---|---|
| Registry + decorator | **Yes** | No | No | No | No | Small teams, monorepo |
| Filesystem discovery | **Yes** | **Yes** | No | No | No | Rapid iteration, dev environments |
| Versioned contracts | **Yes** | No | No | No | **Yes** | Multi-team plugin ecosystem |
| Per-tenant routing | **Yes** | No | **Yes** | No | No | SaaS with tenant customization |
| Lifecycle plugin | **Yes** | No | No | **Yes** | No | Plugins needing DB/model init |
| Remote HTTP plugins | **Yes** | **Yes** (re-register URL) | **Yes** | External | **Yes** | Microservice plugin architecture |
