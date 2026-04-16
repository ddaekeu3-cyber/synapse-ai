---
title: "Agent Doesn't Implement Connection Pooling for LLM API Calls"
description: "Agents that open a new HTTP connection for every LLM API call pay TCP handshake and TLS negotiation overhead on every request — typically 50–200ms per call at the start of each turn. Implement HTTP connection pooling that reuses keep-alive connections across requests, limits total open connections, and monitors pool health so connection exhaustion is visible before it causes timeouts."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-connection-pooling-for-llm-api-calls
tags: [connection-pooling, http-keepalive, latency-reduction, llm-api, tls-overhead, pool-management]
symptoms:
  - "First token latency is consistently 100–200ms higher than expected"
  - "Each turn opens a new TCP connection visible in network traces"
  - "High CPU on TLS handshake when traffic spikes"
  - "No per-host connection limit — ephemeral port exhaustion under load"
  - "Connection errors spike when many concurrent requests share no pool"
---

## Why This Happens

Python's default `httpx.Client` or `requests.Session` without explicit pool configuration creates connections on demand and may not reuse them efficiently across async calls. Each new connection pays TCP slow-start, TLS 1.3 handshake (1 RTT), and HTTP/2 SETTINGS frame exchange before the first byte of the actual request. With connection pooling, the first request pays the full setup cost; subsequent requests reuse the warm connection and skip to the application data exchange.

## Solution 1: Pool Configuration

```python
from dataclasses import dataclass
from typing import Optional


@dataclass
class ConnectionPoolConfig:
    host: str
    max_connections: int = 20           # max open connections to this host
    max_keepalive_connections: int = 10 # max idle keep-alive connections
    keepalive_expiry_seconds: float = 30.0
    connect_timeout_seconds: float = 5.0
    read_timeout_seconds: float = 60.0
    write_timeout_seconds: float = 10.0
    pool_timeout_seconds: float = 5.0   # time to wait for a free connection slot
    http2: bool = True                  # prefer HTTP/2 multiplexing
    retries: int = 0                    # connection-level retries (not request retries)

    def __post_init__(self) -> None:
        if self.max_keepalive_connections > self.max_connections:
            raise ValueError("max_keepalive_connections cannot exceed max_connections")
```

## Solution 2: Pooled HTTP Client

```python
import asyncio
from typing import Any, Dict, Optional

try:
    import httpx
    _HTTPX_AVAILABLE = True
except ImportError:
    _HTTPX_AVAILABLE = False


class PooledLLMHttpClient:
    """
    Wraps httpx.AsyncClient with explicit pool limits and timeouts.
    One instance should be shared across all LLM calls in a process.
    Exposes pool stats for observability.
    """

    def __init__(self, config: ConnectionPoolConfig) -> None:
        if not _HTTPX_AVAILABLE:
            raise ImportError("httpx is required: pip install httpx[http2]")

        self._config = config
        self._client: Optional[httpx.AsyncClient] = None
        self._request_count = 0
        self._error_count = 0
        self._reused_connections = 0

    async def __aenter__(self) -> "PooledLLMHttpClient":
        await self._open()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    async def _open(self) -> None:
        cfg = self._config
        limits = httpx.Limits(
            max_connections=cfg.max_connections,
            max_keepalive_connections=cfg.max_keepalive_connections,
            keepalive_expiry=cfg.keepalive_expiry_seconds,
        )
        timeout = httpx.Timeout(
            connect=cfg.connect_timeout_seconds,
            read=cfg.read_timeout_seconds,
            write=cfg.write_timeout_seconds,
            pool=cfg.pool_timeout_seconds,
        )
        self._client = httpx.AsyncClient(
            limits=limits,
            timeout=timeout,
            http2=cfg.http2,
        )

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def post(
        self,
        url: str,
        headers: Dict[str, str],
        json: Dict[str, Any],
    ) -> httpx.Response:
        if self._client is None:
            await self._open()
        self._request_count += 1
        try:
            response = await self._client.post(url, headers=headers, json=json)
            response.raise_for_status()
            return response
        except Exception:
            self._error_count += 1
            raise

    def stats(self) -> dict:
        pool_info = {}
        if self._client:
            # httpx exposes internal pool state via _transport
            transport = getattr(self._client, "_transport", None)
            if transport and hasattr(transport, "_pool"):
                pool = transport._pool
                pool_info = {
                    "connections": len(getattr(pool, "_connections", [])),
                }
        return {
            "host": self._config.host,
            "max_connections": self._config.max_connections,
            "max_keepalive": self._config.max_keepalive_connections,
            "request_count": self._request_count,
            "error_count": self._error_count,
            "error_rate": round(self._error_count / max(self._request_count, 1), 4),
            **pool_info,
        }
```

## Solution 3: Per-Provider Pool Registry

```python
from typing import Dict


class LLMConnectionPoolRegistry:
    """
    Maintains one PooledLLMHttpClient per LLM provider.
    Ensures pool is opened once and reused across all requests in a process.
    """

    def __init__(self) -> None:
        self._pools: Dict[str, PooledLLMHttpClient] = {}
        self._configs: Dict[str, ConnectionPoolConfig] = {}

    def register(self, provider: str, config: ConnectionPoolConfig) -> None:
        self._configs[provider] = config

    async def get(self, provider: str) -> PooledLLMHttpClient:
        if provider not in self._pools:
            config = self._configs.get(provider)
            if config is None:
                raise KeyError(f"No pool config registered for provider '{provider}'")
            client = PooledLLMHttpClient(config)
            await client._open()
            self._pools[provider] = client
        return self._pools[provider]

    async def close_all(self) -> None:
        for client in self._pools.values():
            await client.close()
        self._pools.clear()

    def all_stats(self) -> Dict[str, dict]:
        return {provider: client.stats() for provider, client in self._pools.items()}
```

## Solution 4: Connection Pool Health Monitor

```python
import time
from typing import List


class ConnectionPoolHealthMonitor:
    """
    Inspects pool stats and alerts when error rates are high
    or when pool configuration may be causing latency (pool too small).
    """

    def __init__(
        self,
        registry: LLMConnectionPoolRegistry,
        error_rate_threshold: float = 0.02,
        min_pool_size_recommendation: int = 10,
    ) -> None:
        self._registry = registry
        self._error_threshold = error_rate_threshold
        self._min_pool = min_pool_size_recommendation

    def check(self) -> List[dict]:
        alerts = []
        for provider, stats in self._registry.all_stats().items():
            if stats["error_rate"] >= self._error_threshold:
                alerts.append({
                    "type": "high_error_rate",
                    "provider": provider,
                    "error_rate": stats["error_rate"],
                    "threshold": self._error_threshold,
                    "severity": "warning",
                })
            if stats["max_connections"] < self._min_pool:
                alerts.append({
                    "type": "small_pool",
                    "provider": provider,
                    "max_connections": stats["max_connections"],
                    "recommendation": f"Increase max_connections to at least {self._min_pool}",
                    "severity": "info",
                })
        return alerts

    def report(self) -> dict:
        return {
            "generated_at": time.time(),
            "pools": self._registry.all_stats(),
            "alerts": self.check(),
        }
```

## Solution 5: Pool-Aware LLM Request Builder

```python
import json
from typing import Any, Dict, List, Optional


class PoolAwareLLMRequestBuilder:
    """
    Constructs and dispatches LLM API requests through the connection pool.
    Encapsulates endpoint construction, header management, and response parsing.
    """

    PROVIDER_ENDPOINTS = {
        "anthropic": "https://api.anthropic.com/v1/messages",
        "openai": "https://api.openai.com/v1/chat/completions",
    }

    def __init__(
        self,
        pool_registry: LLMConnectionPoolRegistry,
        api_keys: Dict[str, str],
    ) -> None:
        self._registry = pool_registry
        self._api_keys = api_keys

    def _build_headers(self, provider: str) -> Dict[str, str]:
        key = self._api_keys.get(provider, "")
        if provider == "anthropic":
            return {
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            }
        return {
            "authorization": f"Bearer {key}",
            "content-type": "application/json",
        }

    async def chat(
        self,
        provider: str,
        model: str,
        messages: List[Dict[str, str]],
        max_tokens: int = 1024,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        client = await self._registry.get(provider)
        url = self.PROVIDER_ENDPOINTS[provider]
        body: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
        }
        if extra:
            body.update(extra)

        response = await client.post(url, headers=self._build_headers(provider), json=body)
        return response.json()
```

## Solution 6: Connection Pool Dashboard

```python
import time


class ConnectionPoolDashboard:
    """
    Aggregates pool stats, health alerts, and configuration
    into a single operational view for all LLM providers.
    """

    def __init__(
        self,
        registry: LLMConnectionPoolRegistry,
        monitor: ConnectionPoolHealthMonitor,
    ) -> None:
        self._registry = registry
        self._monitor = monitor

    def render(self) -> dict:
        all_stats = self._registry.all_stats()
        alerts = self._monitor.check()

        total_requests = sum(s["request_count"] for s in all_stats.values())
        total_errors = sum(s["error_count"] for s in all_stats.values())

        return {
            "generated_at": time.time(),
            "summary": {
                "providers": list(all_stats.keys()),
                "total_requests": total_requests,
                "total_errors": total_errors,
                "fleet_error_rate": round(total_errors / max(total_requests, 1), 4),
            },
            "pools": all_stats,
            "active_alerts": alerts,
        }
```

## Comparison

| Approach | Keep-Alive Reuse | Per-Provider Isolation | Health Monitoring | Request Building | Dashboard |
|---|---|---|---|---|---|
| PooledLLMHttpClient | Yes (httpx limits) | No | No | No | No |
| LLMConnectionPoolRegistry | Via client | Yes | No | No | No |
| ConnectionPoolHealthMonitor | No | No | Yes | No | No |
| PoolAwareLLMRequestBuilder | Via registry | Via registry | No | Yes | No |
| ConnectionPoolDashboard | No | No | Via monitor | No | Yes |

**Best for production**: Create one `LLMConnectionPoolRegistry` at process startup and inject it everywhere — never instantiate `PooledLLMHttpClient` per request. Set `max_connections=20` for production Anthropic workloads (the API rate-limits well before that) and `max_keepalive_connections=10` to keep idle connections alive for burst absorption. Enable `http2=True` — HTTP/2 multiplexing lets multiple requests share one TCP connection, eliminating head-of-line blocking. Shut down the registry cleanly on process exit with `close_all()` to send proper FIN packets and avoid TIME_WAIT accumulation on the server side.
