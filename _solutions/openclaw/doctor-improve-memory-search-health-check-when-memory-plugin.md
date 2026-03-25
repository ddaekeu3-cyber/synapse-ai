---
layout: solution
title: "doctor: improve memory search health check when memory plugin slot is active"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/46878
---

# doctor: improve memory search health check when memory plugin slot is active

## 증상
When a memory plugin (e.g. `memory-neo4j`) owns the `plugins.slots.memory` slot, `openclaw doctor --fix` shows:

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
In `src/commands/doctor-memory-search.ts`, `noteMemorySearchHealth()` returns early with the misleading note when `memoryPluginSlot` is set:

```ts
// Line ~35
const memoryPluginSlot = cfg.plugins?.slots?.memory;
if (memoryPluginSlot && memoryPluginSlot !== 'none') {
  return null;  // ← currently returns null → triggers the misleading note
}
```

The fix should:
- Return early **silently** (no note) when a plugin is active and presumably healthy
- Optionally probe the gateway's memory plugin health endpoint (same `gatewayMemoryProbe` pattern already used for remote providers)
- Use `detectMem

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/46878
