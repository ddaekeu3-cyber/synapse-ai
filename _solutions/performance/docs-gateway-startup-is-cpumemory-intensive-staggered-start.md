---
layout: solution
title: "[Docs]: Gateway startup is CPU/memory-intensive — staggered start recommended for multi-instance hosts"
category: performance
source: https://github.com/openclaw/openclaw/issues/27946
description: "Starting 3+ OpenClaw gateway instances simultaneously on a 2-4 vCPU host saturates CPU and can make the host unresponsive (SSH hangs, other services"
---

# [Docs]: Gateway startup is CPU/memory-intensive — staggered start recommended for multi-instance hosts

## 증상
Starting 3+ OpenClaw gateway instances simultaneously on a 2-4 vCPU host saturates CPU and can make the host unresponsive (SSH hangs, other services timeout). Each gateway consumes approximately 800MB during initialization.

## 원인
Resource bottleneck (CPU, memory, I/O, or network latency) or inefficient algorithm causing timeout or slowdown.

## 해결법
Start containers one at a time with health check gates:
```bash
for container in agent-1 agent-2 agent-3; do
  docker start "$container"
  until curl -sf "http://127.0.0.1:${port}/" > /dev/null 2>&1; do sleep 5; done
  echo "$container healthy"
done
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/27946
