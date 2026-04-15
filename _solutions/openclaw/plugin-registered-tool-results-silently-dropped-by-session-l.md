---
layout: solution
title: "Plugin-registered tool results silently dropped by session layer (memory_forget, memory_store_batch)"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/47573
description: "Plugin-registered tools that override built-in memory tools (, ) have their tool results silently dropped by the OpenClaw session layer. The agent becomes"
---

# Plugin-registered tool results silently dropped by session layer (memory_forget, memory_store_batch)

## 증상
Plugin-registered tools that override built-in memory tools (`memory_forget`, `memory_store_batch`) have their tool results silently dropped by the OpenClaw session layer. The agent becomes unresponsive and requires a manual gateway restart to recover.

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
Delete memories directly via Memgraph CLI:
```bash
echo "MATCH (m:Memory {uuid: \"...\") DETACH DELETE m;" | docker exec -i memgraph mgconsole
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/47573
