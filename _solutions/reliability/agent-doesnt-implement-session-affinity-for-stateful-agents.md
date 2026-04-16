---
title: "Agent Doesn't Implement Session Affinity for Stateful Agents"
description: "Stateful agents that hold conversation history in memory fail when load balancers route follow-up requests to a different instance — session affinity pins each user session to the same agent process."
difficulty: intermediate
category: reliability
tags: [reliability, session-affinity, load-balancing, stateful, distributed, sticky-sessions]
---

# Agent Doesn't Implement Session Affinity for Stateful Agents

## Problem

An agent that keeps conversation history in local memory works perfectly on a single instance. Under load, a round-robin load balancer routes request N+1 to a different instance that has no history of request N. The agent replies as if it's starting fresh, breaking multi-turn conversations and confusing users. Stateful agents require either session affinity (sticky sessions) or externalized state — or both for resilience.

**Symptoms:**
- Second message in a conversation gets "I don't have context from our previous exchange"
- Multi-turn task agents forget earlier steps when instances restart
- Load balancer metrics show perfect distribution but user experience is broken
- Conversation history diverges between instances after horizontal scale-out
- A/B testing shows different quality depending on which instance handled turn 1

---

## Solution 1: Consistent Hash Router by Session ID

Route requests to instances using consistent hashing on the session ID — same session always maps to the same instance.

```python
import asyncio
import hashlib
from dataclasses import dataclass, field
from typing import Optional
import anthropic


@dataclass
class AgentInstance:
    instance_id: str
    history: list[dict] = field(default_factory=list)

    def add_turn(self, user: str, assistant: str) -> None:
        self.history.append({"role": "user", "content": user})
        self.history.append({"role": "assistant", "content": assistant})


class ConsistentHashRouter:
    def __init__(self, instances: list[AgentInstance], virtual_nodes: int = 150):
        self._ring: list[tuple[int, AgentInstance]] = []
        for inst in instances:
            for i in range(virtual_nodes):
                key = f"{inst.instance_id}:{i}".encode()
                h = int(hashlib.md5(key).hexdigest(), 16)
                self._ring.append((h, inst))
        self._ring.sort(key=lambda x: x[0])

    def get_instance(self, session_id: str) -> AgentInstance:
        h = int(hashlib.md5(session_id.encode()).hexdigest(), 16)
        for ring_hash, inst in self._ring:
            if h <= ring_hash:
                return inst
        return self._ring[0][1]  # Wrap around


class AffinityRouter:
    def __init__(self, api_key: str, num_instances: int = 4):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)
        instances = [AgentInstance(instance_id=f"agent-{i}") for i in range(num_instances)]
        self.router = ConsistentHashRouter(instances)

    async def chat(self, session_id: str, user_message: str) -> str:
        instance = self.router.get_instance(session_id)

        response = await self.client.messages.create(
            model="claude-opus-4-6",
            max_tokens=256,
            messages=instance.history + [{"role": "user", "content": user_message}],
        )
        reply = response.content[0].text
        instance.add_turn(user_message, reply)

        print(
            f"[affinity] session={session_id} → instance={instance.instance_id} "
            f"history_turns={len(instance.history)//2}"
        )
        return reply


async def demo():
    router = AffinityRouter(api_key="sk-...")
    sessions = ["sess_alice", "sess_bob", "sess_carol"]

    for turn in range(3):
        for sid in sessions:
            reply = await router.chat(sid, f"Turn {turn}: hello from {sid}")
            print(f"{sid} turn={turn}: {reply[:40]}")

# asyncio.run(demo())
```

---

## Solution 2: Redis-Backed Session Store with Local Cache

Store conversation history in Redis; cache it locally per-process to reduce round-trips on consecutive turns.

```python
import asyncio
import json
import time
from dataclasses import dataclass
from typing import Optional
import anthropic

# pip install redis[asyncio]
try:
    import redis.asyncio as aioredis
except ImportError:
    aioredis = None  # type: ignore


@dataclass
class SessionEntry:
    history: list[dict]
    last_accessed: float


class HybridSessionStore:
    """Redis as source of truth; in-process LRU cache for hot sessions."""

    def __init__(self, redis_url: str = "redis://localhost:6379", ttl: int = 3600):
        self._redis = aioredis.from_url(redis_url, decode_responses=True)
        self._local: dict[str, SessionEntry] = {}
        self._ttl = ttl
        self._local_limit = 200  # Max in-process sessions

    async def load(self, session_id: str) -> list[dict]:
        if session_id in self._local:
            self._local[session_id].last_accessed = time.time()
            return self._local[session_id].history

        raw = await self._redis.get(f"session:{session_id}")
        history = json.loads(raw) if raw else []
        self._evict_if_needed()
        self._local[session_id] = SessionEntry(history=history, last_accessed=time.time())
        return history

    async def save(self, session_id: str, history: list[dict]) -> None:
        if session_id in self._local:
            self._local[session_id].history = history
            self._local[session_id].last_accessed = time.time()
        await self._redis.setex(
            f"session:{session_id}", self._ttl, json.dumps(history)
        )

    def _evict_if_needed(self) -> None:
        if len(self._local) >= self._local_limit:
            oldest = min(self._local, key=lambda k: self._local[k].last_accessed)
            del self._local[oldest]

    async def close(self) -> None:
        await self._redis.aclose()


class RedisAffinityAgent:
    def __init__(self, api_key: str, redis_url: str = "redis://localhost:6379"):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)
        self.store = HybridSessionStore(redis_url)

    async def chat(self, session_id: str, user_message: str) -> str:
        history = await self.store.load(session_id)
        history.append({"role": "user", "content": user_message})

        response = await self.client.messages.create(
            model="claude-opus-4-6",
            max_tokens=256,
            messages=history,
        )
        reply = response.content[0].text
        history.append({"role": "assistant", "content": reply})

        await self.store.save(session_id, history)
        print(f"[redis] session={session_id} turns={len(history)//2}")
        return reply

    async def close(self) -> None:
        await self.store.close()


async def demo():
    agent = RedisAffinityAgent(api_key="sk-...")
    sid = "sess_multi_instance_test"
    r1 = await agent.chat(sid, "My name is Alice.")
    r2 = await agent.chat(sid, "What's my name?")
    print(f"R1: {r1[:40]}")
    print(f"R2: {r2[:40]}")
    await agent.close()

# asyncio.run(demo())
```

---

## Solution 3: Affinity Token in Response Header

Return a signed affinity token in the response header; the client sends it back on the next request so any upstream proxy can use it for routing.

```python
import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass, field
from typing import Optional
from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse
import anthropic

app = FastAPI()
client = anthropic.AsyncAnthropic(api_key="sk-...")

AFFINITY_SECRET = b"affinity-routing-secret"
INSTANCE_ID = secrets.token_hex(4)  # Unique per process

_sessions: dict[str, list[dict]] = {}


def make_affinity_token(session_id: str) -> str:
    """Signed token: instance_id.session_id.ts.sig"""
    ts = int(time.time())
    payload = f"{INSTANCE_ID}.{session_id}.{ts}"
    sig = hmac.new(AFFINITY_SECRET, payload.encode(), hashlib.sha256).hexdigest()[:12]
    return f"{payload}.{sig}"


def parse_affinity_token(token: str) -> Optional[tuple[str, str]]:
    """Returns (instance_id, session_id) if signature valid."""
    try:
        parts = token.rsplit(".", 1)
        payload, submitted_sig = parts[0], parts[1]
        expected_sig = hmac.new(AFFINITY_SECRET, payload.encode(), hashlib.sha256).hexdigest()[:12]
        if not hmac.compare_digest(expected_sig, submitted_sig):
            return None
        p_parts = payload.split(".")
        return p_parts[0], p_parts[1]  # instance_id, session_id
    except Exception:
        return None


@app.post("/agent/chat")
async def chat(
    request: Request,
    x_affinity_token: Optional[str] = Header(default=None, alias="X-Affinity-Token"),
    x_session_id: Optional[str] = Header(default=None, alias="X-Session-ID"),
):
    session_id = x_session_id or secrets.token_hex(8)

    # If affinity token points to a different instance, client should have been routed there.
    # Here we handle gracefully by loading from shared store (or starting fresh).
    if x_affinity_token:
        parsed = parse_affinity_token(x_affinity_token)
        if parsed:
            token_instance, token_session = parsed
            if token_instance != INSTANCE_ID:
                print(f"[affinity] Misrouted: token instance={token_instance}, this={INSTANCE_ID}")
                # Load from shared store or start fresh (in prod: Redis)
                _sessions.setdefault(token_session, [])
                session_id = token_session

    history = _sessions.setdefault(session_id, [])
    body = await request.json()
    user_message = body.get("message", "")
    history.append({"role": "user", "content": user_message})

    response = await client.messages.create(
        model="claude-opus-4-6",
        max_tokens=256,
        messages=history,
    )
    reply = response.content[0].text
    history.append({"role": "assistant", "content": reply})

    affinity_token = make_affinity_token(session_id)
    return JSONResponse(
        {"reply": reply, "session_id": session_id},
        headers={
            "X-Affinity-Token": affinity_token,
            "X-Instance-ID": INSTANCE_ID,
        },
    )
```

---

## Solution 4: Warm Handoff on Instance Shutdown

When an instance is shutting down, transfer active sessions to a peer before draining.

```python
import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Optional
import aiohttp
import anthropic


@dataclass
class Session:
    session_id: str
    history: list[dict] = field(default_factory=list)
    last_active: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {"session_id": self.session_id, "history": self.history}


class WarmHandoffAgent:
    def __init__(self, api_key: str, instance_id: str, peer_urls: list[str]):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)
        self.instance_id = instance_id
        self.peer_urls = peer_urls
        self._sessions: dict[str, Session] = {}
        self._shutting_down = False
        self._active_requests = 0
        self._lock = asyncio.Lock()

    async def chat(self, session_id: str, message: str) -> str:
        if self._shutting_down:
            raise RuntimeError("instance shutting down")

        async with self._lock:
            self._active_requests += 1

        try:
            session = self._sessions.setdefault(
                session_id, Session(session_id=session_id)
            )
            session.history.append({"role": "user", "content": message})
            session.last_active = time.time()

            response = await self.client.messages.create(
                model="claude-opus-4-6",
                max_tokens=256,
                messages=session.history,
            )
            reply = response.content[0].text
            session.history.append({"role": "assistant", "content": reply})
            return reply
        finally:
            async with self._lock:
                self._active_requests -= 1

    async def graceful_shutdown(self, drain_timeout: float = 30.0) -> None:
        self._shutting_down = True
        print(f"[handoff] {self.instance_id} initiating graceful shutdown")

        # Wait for in-flight requests
        deadline = time.monotonic() + drain_timeout
        while self._active_requests > 0 and time.monotonic() < deadline:
            await asyncio.sleep(0.1)

        # Transfer sessions to a peer
        if self._sessions and self.peer_urls:
            peer = self.peer_urls[0]
            payload = [s.to_dict() for s in self._sessions.values()]
            try:
                async with aiohttp.ClientSession() as http:
                    await http.post(
                        f"{peer}/admin/receive-sessions",
                        json={"sessions": payload, "from_instance": self.instance_id},
                        timeout=aiohttp.ClientTimeout(total=5.0),
                    )
                print(f"[handoff] Transferred {len(payload)} sessions to {peer}")
            except Exception as exc:
                print(f"[handoff] Transfer failed: {exc} — sessions lost")

    async def receive_sessions(self, sessions_data: list[dict]) -> None:
        """Accept sessions migrated from a shutting-down peer."""
        for s in sessions_data:
            sid = s["session_id"]
            if sid not in self._sessions:
                self._sessions[sid] = Session(
                    session_id=sid, history=s.get("history", [])
                )
                print(f"[handoff] Received session {sid} ({len(s['history'])//2} turns)")
```

---

## Solution 5: Session State Externalized to PostgreSQL

Store the full conversation history in PostgreSQL; any instance can resume any session.

```python
import asyncio
import json
import time
from typing import Optional
import anthropic

# pip install asyncpg
try:
    import asyncpg
except ImportError:
    asyncpg = None  # type: ignore


class PostgresSessionStore:
    CREATE_TABLE = """
    CREATE TABLE IF NOT EXISTS agent_sessions (
        session_id TEXT PRIMARY KEY,
        history    JSONB NOT NULL DEFAULT '[]',
        created_at DOUBLE PRECISION NOT NULL,
        updated_at DOUBLE PRECISION NOT NULL
    )
    """

    def __init__(self, dsn: str):
        self._dsn = dsn
        self._pool: Optional[asyncpg.Pool] = None

    async def connect(self) -> None:
        self._pool = await asyncpg.create_pool(self._dsn, min_size=2, max_size=10)
        async with self._pool.acquire() as conn:
            await conn.execute(self.CREATE_TABLE)

    async def load(self, session_id: str) -> list[dict]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT history FROM agent_sessions WHERE session_id = $1",
                session_id,
            )
        return json.loads(row["history"]) if row else []

    async def save(self, session_id: str, history: list[dict]) -> None:
        now = time.time()
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO agent_sessions (session_id, history, created_at, updated_at)
                VALUES ($1, $2::jsonb, $3, $3)
                ON CONFLICT (session_id) DO UPDATE
                SET history = $2::jsonb, updated_at = $3
                """,
                session_id,
                json.dumps(history),
                now,
            )

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()


class PostgresAffinityAgent:
    def __init__(self, api_key: str, pg_dsn: str):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)
        self.store = PostgresSessionStore(pg_dsn)

    async def start(self) -> None:
        await self.store.connect()

    async def chat(self, session_id: str, message: str) -> str:
        history = await self.store.load(session_id)
        history.append({"role": "user", "content": message})

        response = await self.client.messages.create(
            model="claude-opus-4-6",
            max_tokens=256,
            messages=history,
        )
        reply = response.content[0].text
        history.append({"role": "assistant", "content": reply})
        await self.store.save(session_id, history)

        print(f"[pg] session={session_id} turns={len(history)//2}")
        return reply

    async def close(self) -> None:
        await self.store.close()


async def demo():
    agent = PostgresAffinityAgent(
        api_key="sk-...",
        pg_dsn="postgresql://user:pass@localhost/agentdb",
    )
    await agent.start()

    sid = "pg_sess_001"
    await agent.chat(sid, "My favorite language is Python.")
    reply = await agent.chat(sid, "What's my favorite language?")
    print(reply)

    await agent.close()

# asyncio.run(demo())
```

---

## Solution 6: Circuit Breaker for Session Migration Failures

When the external session store is unavailable, fall back to in-process state and signal the load balancer to maintain affinity.

```python
import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
import anthropic


class StoreState(Enum):
    OK = "ok"
    DEGRADED = "degraded"      # Using local fallback
    RECOVERING = "recovering"


@dataclass
class LocalSession:
    history: list[dict] = field(default_factory=list)
    is_local_only: bool = False  # True when external store is unavailable


class ResilientSessionAgent:
    def __init__(self, api_key: str):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)
        self._local: dict[str, LocalSession] = {}
        self._store_state = StoreState.OK
        self._store_failures = 0
        self._failure_threshold = 3
        self._recovery_interval = 30.0
        self._last_failure: float = 0

    def _should_use_external(self) -> bool:
        if self._store_state == StoreState.OK:
            return True
        if self._store_state == StoreState.RECOVERING:
            if time.time() - self._last_failure > self._recovery_interval:
                self._store_state = StoreState.OK
                self._store_failures = 0
                print("[session] External store recovered")
                return True
        return False

    async def _load_from_external(self, session_id: str) -> Optional[list[dict]]:
        # Simulate Redis/Postgres load
        await asyncio.sleep(0.01)
        return None  # Would return stored history in production

    async def _save_to_external(self, session_id: str, history: list[dict]) -> None:
        await asyncio.sleep(0.01)  # Simulate save

    async def chat(self, session_id: str, message: str) -> dict:
        session = self._local.setdefault(session_id, LocalSession())

        if self._should_use_external() and not session.is_local_only:
            try:
                external_history = await self._load_from_external(session_id)
                if external_history is not None:
                    session.history = external_history
            except Exception:
                self._store_failures += 1
                if self._store_failures >= self._failure_threshold:
                    self._store_state = StoreState.DEGRADED
                    session.is_local_only = True
                    print(f"[session] Store circuit OPEN — session {session_id} now local-only")
                self._last_failure = time.time()

        session.history.append({"role": "user", "content": message})
        response = await self.client.messages.create(
            model="claude-opus-4-6",
            max_tokens=256,
            messages=session.history,
        )
        reply = response.content[0].text
        session.history.append({"role": "assistant", "content": reply})

        if self._should_use_external() and not session.is_local_only:
            try:
                await self._save_to_external(session_id, session.history)
            except Exception:
                session.is_local_only = True

        return {
            "reply": reply,
            "store_state": self._store_state.value,
            "local_only": session.is_local_only,
        }


async def demo():
    agent = ResilientSessionAgent(api_key="sk-...")
    result = await agent.chat("sess_1", "Hello!")
    print(f"State: {result['store_state']}, Local: {result['local_only']}")

# asyncio.run(demo())
```

---

## Comparison

| Solution | State Location | Multi-Instance | Failover | Complexity | Best For |
|---|---|---|---|---|---|
| Consistent hash router | In-process | Partial (hash-based) | No | Low | Small fixed clusters |
| Redis + local cache | Redis | Yes | On Redis failure: none | Medium | Most production deployments |
| Affinity token in header | In-process | Via proxy routing | No | Low | Proxy-aware infrastructure |
| Warm handoff on shutdown | In-process + peer transfer | Manual migration | Partial | High | Long-lived sessions |
| PostgreSQL store | Postgres | Yes | On DB failure: none | Medium | Audit requirements |
| Circuit breaker fallback | Local + external | Degraded mode | Yes (local-only) | Medium | Resilience-critical agents |

**Recommendation:** Start with Solution 2 (Redis + local cache) for most production deployments — it provides true multi-instance state sharing with fast local reads for consecutive turns. Add Solution 6 (circuit breaker) on top so a Redis outage degrades gracefully to local-only mode rather than crashing all sessions.
