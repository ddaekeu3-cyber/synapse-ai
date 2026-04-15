---
layout: solution
title: "Non-default agents do not inherit agents.defaults.heartbeat unless they define agents.list[].heartbeat"
category: hallucination
source: https://github.com/openclaw/openclaw/issues/49613
description: "Behavior bug (incorrect output/state without"
---

# Non-default agents do not inherit agents.defaults.heartbeat unless they define agents.list[].heartbeat

## 증상
Behavior bug (incorrect output/state without crash)

## 원인
Model generated plausible but incorrect output due to insufficient grounding, missing verification, or high sampling temperature.

## 해결법
corepack pnpm exec vitest run \
  src/infra/heartbeat-runner.returns-default-unset.test.ts \
  src/commands/health.snapshot.test.ts

Test Files  2 passed (2)
Tests      32 passed (32)
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/49613
