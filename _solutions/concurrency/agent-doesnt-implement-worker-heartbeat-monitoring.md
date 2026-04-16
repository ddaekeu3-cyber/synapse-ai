---
layout: solution
title: "Agent Doesn't Implement Worker Heartbeat Monitoring"
category: concurrency
description: "Have workers emit periodic heartbeats and have a monitor detect missing beats — so stuck, crashed, or hung workers are detected and restarted automatically."
tags: [concurrency, heartbeat, monitoring, worker, health-check, python]
---

# Agent Doesn't Implement Worker Heartbeat Monitoring

Workers that go silent look identical to workers that are busy. Without heartbeats, a hung worker holds its task indefinitely while the queue starves. Heartbeat monitoring detects silence, marks the worker as dead, and returns its task to the queue for reassignment.

## Option 1: asyncio Heartbeat with Monitor Task

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()

@dataclass
class WorkerState:
    worker_id: str
    last_beat: float = field(default_factory=time.monotonic)
    alive: bool = True
    current_task: str = ""

    def beat(self):
        self.last_beat = time.monotonic()

    def age(self) -> float:
        return time.monotonic() - self.last_beat

WORKERS: dict[str, WorkerState] = {}
HEARTBEAT_INTERVAL = 1.0  # seconds
DEAD_THRESHOLD = 5.0      # seconds without heartbeat = dead

async def worker(worker_id: str, task: str):
    state = WorkerState(worker_id=worker_id, current_task=task)
    WORKERS[worker_id] = state
    print(f"[{worker_id}] Starting task: {task[:40]}")

    # Heartbeat loop runs alongside the actual work
    async def heartbeat_loop():
        while state.alive:
            state.beat()
            await asyncio.sleep(HEARTBEAT_INTERVAL)

    beat_task = asyncio.create_task(heartbeat_loop())

    try:
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            messages=[{"role": "user", "content": task}],
        )
        print(f"[{worker_id}] Done: {resp.content[0].text[:50]}")
        return resp.content[0].text
    finally:
        state.alive = False
        beat_task.cancel()
        WORKERS.pop(worker_id, None)

async def monitor():
    """Periodically check all workers for missed heartbeats."""
    while True:
        await asyncio.sleep(2.0)
        now = time.monotonic()
        dead = [w for w in list(WORKERS.values()) if w.age() > DEAD_THRESHOLD]
        for w in dead:
            print(f"[MONITOR] Worker {w.worker_id} is DEAD (silent for {w.age():.1f}s task={w.current_task[:30]})")
            WORKERS.pop(w.worker_id, None)
        if not dead:
            alive = list(WORKERS.keys())
            if alive:
                print(f"[MONITOR] All workers healthy: {alive}")

async def main():
    monitor_task = asyncio.create_task(monitor())
    tasks = [
        worker("w-1", "Explain asyncio event loops in Python"),
        worker("w-2", "What is the GIL?"),
        worker("w-3", "Explain Python decorators"),
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    monitor_task.cancel()
    for i, r in enumerate(results, 1):
        print(f"Worker {i}: {'ERROR' if isinstance(r, Exception) else r[:50]}")

asyncio.run(main())

# Expected Token Savings: N/A — prevents wasted queue slots on silent workers
# Environment: asyncio; DEAD_THRESHOLD tunable to your task's expected duration
```

## Option 2: SQLite-Based Heartbeat Registry for Multi-Process Workers

```python
import anthropic
import sqlite3
import time
import threading
import uuid
import multiprocessing as mp

client = anthropic.Anthropic()
DB = "worker_heartbeats.db"

def init_db():
    con = sqlite3.connect(DB)
    con.execute("""
        CREATE TABLE IF NOT EXISTS workers (
            worker_id TEXT PRIMARY KEY,
            task TEXT, last_beat REAL,
            status TEXT DEFAULT 'alive',
            started_at REAL
        )
    """)
    con.commit(); con.close()

def register_worker(worker_id: str, task: str):
    con = sqlite3.connect(DB)
    now = time.time()
    con.execute("INSERT OR REPLACE INTO workers VALUES (?,?,?,?,?)",
                (worker_id, task, now, "alive", now))
    con.commit(); con.close()

def send_heartbeat(worker_id: str):
    con = sqlite3.connect(DB)
    con.execute("UPDATE workers SET last_beat=? WHERE worker_id=?",
                (time.time(), worker_id))
    con.commit(); con.close()

def mark_complete(worker_id: str):
    con = sqlite3.connect(DB)
    con.execute("UPDATE workers SET status='complete' WHERE worker_id=?", (worker_id,))
    con.commit(); con.close()

def find_dead_workers(threshold_s: float = 10.0) -> list[dict]:
    cutoff = time.time() - threshold_s
    con = sqlite3.connect(DB)
    rows = con.execute(
        "SELECT worker_id, task, last_beat FROM workers WHERE status='alive' AND last_beat<?",
        (cutoff,)
    ).fetchall()
    con.close()
    return [{"id": r[0], "task": r[1], "silent_s": time.time() - r[2]} for r in rows]

def evict_dead(worker_id: str):
    con = sqlite3.connect(DB)
    con.execute("UPDATE workers SET status='dead' WHERE worker_id=?", (worker_id,))
    con.commit(); con.close()

def worker_fn(worker_id: str, task: str):
    """Worker with background heartbeat thread."""
    register_worker(worker_id, task)
    stop_event = threading.Event()

    def beat_loop():
        while not stop_event.is_set():
            send_heartbeat(worker_id)
            stop_event.wait(timeout=2.0)

    beat_thread = threading.Thread(target=beat_loop, daemon=True)
    beat_thread.start()

    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            messages=[{"role": "user", "content": task}],
        )
        print(f"[{worker_id}] Completed: {resp.content[0].text[:50]}")
    finally:
        stop_event.set()
        mark_complete(worker_id)

def monitor_loop(check_interval: float = 5.0, threshold: float = 10.0):
    while True:
        time.sleep(check_interval)
        dead = find_dead_workers(threshold)
        for w in dead:
            print(f"[MONITOR] DEAD worker {w['id']}: silent {w['silent_s']:.1f}s task={w['task'][:40]}")
            evict_dead(w["id"])

init_db()
# Start monitor in background thread
monitor_thread = threading.Thread(target=monitor_loop, daemon=True)
monitor_thread.start()

# Run workers in threads
threads = []
for i, task in enumerate(["What is a semaphore?", "Explain mutex locks", "What is a deadlock?"], 1):
    wid = f"worker-{i}"
    t = threading.Thread(target=worker_fn, args=(wid, task))
    threads.append(t)
    t.start()

for t in threads:
    t.join()

print("All workers complete.")

# Expected Token Savings: SQLite registry works across processes/hosts; evict frees queue slots
# Environment: SQLite; replace with Redis for distributed multi-host worker pools
```

## Option 3: Async Heartbeat with Task Reassignment Queue

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()

@dataclass
class Task:
    task_id: str
    payload: str
    attempts: int = 0
    max_attempts: int = 3

TASK_QUEUE: asyncio.Queue[Task] = asyncio.Queue()
DEAD_LETTER: list[Task] = []
REGISTRY: dict[str, dict] = {}  # worker_id -> {beat, task_id, task}

BEAT_INTERVAL = 1.5
DEAD_THRESHOLD = 6.0

async def monitored_worker(worker_id: str):
    """Worker that pulls tasks from queue, sends heartbeats, processes tasks."""
    while True:
        task = await TASK_QUEUE.get()
        task.attempts += 1
        REGISTRY[worker_id] = {
            "beat": time.monotonic(),
            "task_id": task.task_id,
            "task": task.payload,
        }
        print(f"[{worker_id}] attempt={task.attempts} task={task.task_id}")

        stop_beat = asyncio.Event()

        async def heartbeat():
            while not stop_beat.is_set():
                REGISTRY[worker_id]["beat"] = time.monotonic()
                try:
                    await asyncio.wait_for(asyncio.sleep(BEAT_INTERVAL), timeout=BEAT_INTERVAL + 0.1)
                except asyncio.TimeoutError:
                    pass

        beat_task = asyncio.create_task(heartbeat())
        try:
            resp = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=128,
                messages=[{"role": "user", "content": task.payload}],
            )
            print(f"[{worker_id}] Done: {resp.content[0].text[:50]}")
        except Exception as e:
            print(f"[{worker_id}] Error: {e}")
            if task.attempts < task.max_attempts:
                await TASK_QUEUE.put(task)
            else:
                DEAD_LETTER.append(task)
        finally:
            stop_beat.set()
            beat_task.cancel()
            REGISTRY.pop(worker_id, None)
            TASK_QUEUE.task_done()

async def heartbeat_monitor():
    while True:
        await asyncio.sleep(3.0)
        now = time.monotonic()
        for wid, info in list(REGISTRY.items()):
            silent = now - info["beat"]
            if silent > DEAD_THRESHOLD:
                print(f"[MONITOR] Worker {wid} DEAD ({silent:.1f}s silent), re-queuing {info['task_id']}")
                REGISTRY.pop(wid, None)
                # Re-queue task (simplified — production needs lock + task ref)
                # Here we just log; full impl would track task reference

async def main():
    tasks = [
        Task("t1", "Explain the producer-consumer pattern"),
        Task("t2", "What is a thread pool?"),
        Task("t3", "Explain async generators in Python"),
        Task("t4", "What are Python coroutines?"),
    ]
    for t in tasks:
        await TASK_QUEUE.put(t)

    monitor = asyncio.create_task(heartbeat_monitor())
    workers = [asyncio.create_task(monitored_worker(f"w{i}")) for i in range(2)]

    await TASK_QUEUE.join()
    monitor.cancel()
    for w in workers:
        w.cancel()

    if DEAD_LETTER:
        print(f"\nDead letter queue: {[t.task_id for t in DEAD_LETTER]}")

asyncio.run(main())

# Expected Token Savings: Failed/hung tasks re-queued; no manual intervention needed
# Environment: asyncio; DEAD_THRESHOLD should exceed your longest expected task duration
```

## Option 4: gRPC-Style Bidirectional Heartbeat Simulation

```python
import anthropic
import asyncio
import time
import uuid
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()

@dataclass
class WorkerConnection:
    worker_id: str
    last_ping: float = field(default_factory=time.monotonic)
    last_pong: float = field(default_factory=time.monotonic)
    missed_pongs: int = 0
    alive: bool = True

    def ping(self):
        self.last_ping = time.monotonic()

    def pong(self):
        self.last_pong = time.monotonic()
        self.missed_pongs = 0

    def check(self, tolerance_s: float = 5.0) -> bool:
        if time.monotonic() - self.last_pong > tolerance_s:
            self.missed_pongs += 1
            return False
        return True

CONNECTIONS: dict[str, WorkerConnection] = {}

async def worker_process(worker_id: str, tasks: list[str]):
    conn = WorkerConnection(worker_id=worker_id)
    CONNECTIONS[worker_id] = conn

    async def pong_loop():
        """Worker side: respond to pings."""
        while conn.alive:
            conn.pong()
            await asyncio.sleep(1.0)

    pong_task = asyncio.create_task(pong_loop())
    try:
        for task in tasks:
            resp = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=64,
                messages=[{"role": "user", "content": task}],
            )
            print(f"[{worker_id}] {task[:30]}: {resp.content[0].text[:40]}")
    finally:
        conn.alive = False
        pong_task.cancel()
        CONNECTIONS.pop(worker_id, None)

async def ping_monitor(interval: float = 2.0, max_missed: int = 3):
    """Monitor side: send pings, check pongs."""
    while True:
        await asyncio.sleep(interval)
        for wid, conn in list(CONNECTIONS.items()):
            conn.ping()
            healthy = conn.check(tolerance_s=interval * 2)
            if not healthy:
                print(f"[PING-MONITOR] {wid}: missed pong #{conn.missed_pongs}")
                if conn.missed_pongs >= max_missed:
                    print(f"[PING-MONITOR] Evicting dead worker: {wid}")
                    conn.alive = False
                    CONNECTIONS.pop(wid, None)
            else:
                print(f"[PING-MONITOR] {wid}: healthy (rtt={time.monotonic()-conn.last_ping:.2f}s)")

async def main():
    monitor = asyncio.create_task(ping_monitor())
    worker_tasks = [
        asyncio.create_task(worker_process("w-alpha", [
            "Explain Python GIL", "What is asyncio?",
        ])),
        asyncio.create_task(worker_process("w-beta", [
            "What is a coroutine?", "Explain event loops",
        ])),
    ]
    await asyncio.gather(*worker_tasks, return_exceptions=True)
    await asyncio.sleep(3)  # Let monitor run one more cycle
    monitor.cancel()

asyncio.run(main())

# Expected Token Savings: N/A; ping/pong pattern detects network partition vs. busy distinction
# Environment: asyncio; extend with actual network transport for distributed workers
```

## Option 5: Watchdog Process with SIGTERM Recovery

```python
import anthropic
import asyncio
import signal
import time
from dataclasses import dataclass

client = anthropic.AsyncAnthropic()

@dataclass
class WatchdogState:
    deadline: float
    worker_id: str
    task: str
    cancelled: bool = False

    def extend(self, seconds: float):
        self.deadline = max(self.deadline, time.monotonic() + seconds)

    def is_expired(self) -> bool:
        return time.monotonic() > self.deadline

WATCHDOG_REGISTRY: dict[str, WatchdogState] = {}

async def watchdog_monitor(check_interval: float = 2.0):
    while True:
        await asyncio.sleep(check_interval)
        for wid, state in list(WATCHDOG_REGISTRY.items()):
            if state.is_expired() and not state.cancelled:
                print(f"[WATCHDOG] Worker {wid} exceeded deadline! Task: {state.task[:40]}")
                state.cancelled = True
                WATCHDOG_REGISTRY.pop(wid, None)

async def worker_with_watchdog(
    worker_id: str,
    task: str,
    timeout_s: float = 30.0,
    heartbeat_interval: float = 5.0,
) -> str:
    state = WatchdogState(
        deadline=time.monotonic() + timeout_s,
        worker_id=worker_id,
        task=task,
    )
    WATCHDOG_REGISTRY[worker_id] = state

    async def heartbeat():
        while worker_id in WATCHDOG_REGISTRY:
            state.extend(heartbeat_interval * 2)  # extend deadline on each beat
            print(f"  [HB] {worker_id}: deadline extended to +{state.deadline - time.monotonic():.1f}s")
            await asyncio.sleep(heartbeat_interval)

    beat_task = asyncio.create_task(heartbeat())
    try:
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            messages=[{"role": "user", "content": task}],
        )
        return resp.content[0].text
    finally:
        beat_task.cancel()
        WATCHDOG_REGISTRY.pop(worker_id, None)

async def main():
    watchdog = asyncio.create_task(watchdog_monitor())
    results = await asyncio.gather(
        worker_with_watchdog("wA", "Explain Python decorators", timeout_s=30),
        worker_with_watchdog("wB", "What is a generator?",      timeout_s=30),
        return_exceptions=True,
    )
    watchdog.cancel()
    for r in results:
        print(f"Result: {r[:80] if isinstance(r, str) else r}")

asyncio.run(main())

# Expected Token Savings: Heartbeat extends deadline only while work progresses; idle workers expire
# Environment: asyncio; extend heartbeat_interval matches expected progress checkpoints
```

## Option 6: Distributed Heartbeat with Leader Election

```python
import anthropic
import asyncio
import sqlite3
import time
import uuid

client = anthropic.AsyncAnthropic()
DB = "cluster_heartbeats.db"

def init_db():
    con = sqlite3.connect(DB)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS nodes (
            node_id TEXT PRIMARY KEY, role TEXT,
            last_beat REAL, tasks_completed INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS leader_lock (
            id INTEGER PRIMARY KEY CHECK (id=1),
            node_id TEXT, acquired_at REAL, expires_at REAL
        );
    """)
    con.execute("INSERT OR IGNORE INTO leader_lock VALUES (1, NULL, 0, 0)")
    con.commit(); con.close()

def register_node(node_id: str, role: str = "worker"):
    con = sqlite3.connect(DB)
    con.execute("INSERT OR REPLACE INTO nodes VALUES (?,?,?,0)", (node_id, role, time.time()))
    con.commit(); con.close()

def heartbeat(node_id: str):
    con = sqlite3.connect(DB)
    con.execute("UPDATE nodes SET last_beat=? WHERE node_id=?", (time.time(), node_id))
    con.commit(); con.close()

def try_become_leader(node_id: str, lease_s: float = 10.0) -> bool:
    now = time.time()
    con = sqlite3.connect(DB)
    # Acquire if lock is expired or unset
    row = con.execute("SELECT node_id, expires_at FROM leader_lock WHERE id=1").fetchone()
    if not row[0] or row[1] < now or row[0] == node_id:
        con.execute("UPDATE leader_lock SET node_id=?, acquired_at=?, expires_at=? WHERE id=1",
                    (node_id, now, now + lease_s))
        con.commit(); con.close()
        return True
    con.close()
    return False

def get_dead_workers(threshold_s: float = 8.0) -> list[str]:
    cutoff = time.time() - threshold_s
    con = sqlite3.connect(DB)
    rows = con.execute(
        "SELECT node_id FROM nodes WHERE role='worker' AND last_beat<?", (cutoff,)
    ).fetchall()
    con.close()
    return [r[0] for r in rows]

async def worker_node(node_id: str, tasks: list[str]):
    register_node(node_id, "worker")
    stop = asyncio.Event()

    async def beat():
        while not stop.is_set():
            heartbeat(node_id)
            await asyncio.sleep(2.0)

    beat_task = asyncio.create_task(beat())
    try:
        for task in tasks:
            resp = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=64,
                messages=[{"role": "user", "content": task}],
            )
            print(f"[{node_id}] {task[:30]}: {resp.content[0].text[:40]}")
    finally:
        stop.set()
        beat_task.cancel()

async def leader_node(node_id: str):
    register_node(node_id, "leader_candidate")
    while True:
        is_leader = try_become_leader(node_id)
        if is_leader:
            heartbeat(node_id)
            dead = get_dead_workers()
            if dead:
                print(f"[LEADER {node_id}] Dead workers detected: {dead}")
            else:
                print(f"[LEADER {node_id}] All workers healthy")
        else:
            print(f"[{node_id}] Not leader — monitoring only")
        await asyncio.sleep(3.0)

async def main():
    init_db()
    leader = asyncio.create_task(leader_node("leader-0"))
    workers = [
        asyncio.create_task(worker_node("w-1", ["What is consensus?", "Explain Raft"])),
        asyncio.create_task(worker_node("w-2", ["What is Paxos?"])),
    ]
    await asyncio.gather(*workers, return_exceptions=True)
    await asyncio.sleep(5)
    leader.cancel()

asyncio.run(main())

# Expected Token Savings: N/A; leader election ensures exactly one monitor; avoids duplicate evictions
# Environment: SQLite for single-host simulation; swap with etcd/ZooKeeper for true distributed setup
```

## Comparison

| Option | Heartbeat Mechanism | Dead Detection | Reassignment |
|--------|--------------------|--------------|----|
| 1 — asyncio Monitor | In-memory dict update | Age threshold | Log only |
| 2 — SQLite Registry | DB timestamp update | Periodic SQL query | Mark evicted |
| 3 — Queue + Monitor | In-memory dict | Age threshold | Re-queue task |
| 4 — Ping/Pong | Bidirectional beat | Missed pong count | Evict connection |
| 5 — Watchdog Deadline | Deadline extension on beat | Expired deadline | Cancel task |
| 6 — Leader Election | SQLite lease lock | Leader-checked beats | Centralized eviction |
