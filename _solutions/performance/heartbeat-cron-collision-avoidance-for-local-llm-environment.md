---
layout: solution
title: "Heartbeat-cron collision avoidance for local LLM environments"
category: performance
source: https://github.com/openclaw/openclaw/issues/50773
description: "When running with a local LLM (e.g. Ollama), concurrent cron jobs and heartbeats compete for the same inference resource. A running cron job can cause"
---

# Heartbeat-cron collision avoidance for local LLM environments

## 증상
When running with a local LLM (e.g. Ollama), concurrent cron jobs and heartbeats compete for the same inference resource. A running cron job can cause heartbeats to timeout or the LLM to respond slowly.

## 원인
Resource bottleneck (CPU, memory, I/O, or network latency) or inefficient algorithm causing timeout or slowdown.

## 해결법
1. **Always-on cron lane check**: Skip heartbeats when a cron job is actively executing (`cron-in-progress`). This is safe for all environments since heartbeats are advisory and the wake layer retries in 1s.

2. **Opt-in `heartbeat.skipWhenBusy` config**: Broadens the check to skip heartbeats when **any** lane (subagent, nested) has active tasks (`lanes-busy`). Useful for resource-constrained environments.

Both new skip reasons use the same proven retry mechanism as `requests-in-flight` — the wake layer retries after 1s, and the scheduler does not advance the schedule.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/50773
