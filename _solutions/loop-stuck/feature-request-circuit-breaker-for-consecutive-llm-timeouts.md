---
layout: solution
title: "Feature request: Circuit breaker for consecutive LLM timeouts"
category: loop-stuck
source: https://github.com/openclaw/openclaw/issues/45389
---

# Feature request: Circuit breaker for consecutive LLM timeouts

## 증상
When an LLM request times out, OpenClaw retries on the next heartbeat/message. If the root cause persists (e.g., context too large, provider degradation), the system retries indefinitely — compounding the failure. We observed 233 timeouts across 13 agents over 10 days, with one agent stuck in a retry loop for 18+ consecutive failures.

## 원인
보고된 버그/문제. 카테고리: loop-stuck.

## 해결법
We currently use a cron-based monitor that checks gateway logs for timeout patterns and resets sessions. This is functional but reactive and adds token cost.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/45389
