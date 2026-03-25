---
layout: solution
title: "[Feature]: heartbeat: add jitter/startDelay to prevent simultaneous triggers across agents"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/33803
---

# [Feature]: heartbeat: add jitter/startDelay to prevent simultaneous triggers across agents

## 증상
When running multiple agents with the same `heartbeat.every` interval, all agents fire simultaneously after gateway restart. This causes request queue buildup on the main lane since all heartbeat turns compete for the same serialized lane.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Using different prime-ish intervals per agent to simulate staggering via natural drift:

```json
{ "id": "chat",           "heartbeat": { "every": "120m" } },
{ "id": "bigdao",         "heartbeat": { "every": "113m" } },
{ "id": "code",           "heartbeat": { "every": "107m" } },
{ "id": "vision",         "heartbeat": { "every": "97m"  } },
{ "id": "firstprinciple", "heartbeat": { "every": "89m"  } },
{ "id": "linus",          "heartbeat": { "every": "83m"  } },
{ "id": "oldmoney",       "heartbeat": { "every": "79m"  } }
```

This works but is unintuitive and requires users to manually reas

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/33803
