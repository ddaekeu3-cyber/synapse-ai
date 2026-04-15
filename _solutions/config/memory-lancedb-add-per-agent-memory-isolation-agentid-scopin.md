---
layout: solution
title: "memory-lancedb: add per-agent memory isolation (agentId scoping)"
category: config
source: https://github.com/openclaw/openclaw/issues/15325
description: "The plugin stores all memories in a single shared LanceDB table with no agent scoping. In multi-agent setups, this causes cross-agent memory bleed — Agent"
---

# memory-lancedb: add per-agent memory isolation (agentId scoping)

## 증상
The `memory-lancedb` plugin stores all memories in a single shared LanceDB table with no agent scoping. In multi-agent setups, this causes **cross-agent memory bleed** — Agent A recalls memories from Agent B's conversations, leading to identity confusion and context mixing.

## 원인
Environment variable, configuration file, or initialization parameter missing, malformed, or incorrectly scoped.

## 해결법
We've patched the bundled plugin locally with the approach above and confirmed it works. The migration runs cleanly on existing data, and agent-scoped recall/capture works as expected.

Happy to contribute a PR if helpful.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/15325
