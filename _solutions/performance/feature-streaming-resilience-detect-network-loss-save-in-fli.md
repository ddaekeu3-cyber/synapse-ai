---
layout: solution
title: "[FEATURE] Streaming Resilience: Detect network loss, save in-flight state, and auto-resume on reconnect"
category: performance
source: https://github.com/anthropics/claude-code/issues/26729
description: "When using Claude Code on an unstable network (WiFi drops, power outages, VPN reconnects, mobile hotspot switching, laptop sleep/wake), a mid-task"
---

# [FEATURE] Streaming Resilience: Detect network loss, save in-flight state, and auto-resume on reconnect

## 증상
When using Claude Code on an unstable network (WiFi drops, power outages, VPN reconnects, mobile hotspot switching, laptop sleep/wake), a mid-task disconnection leads to a cascade of problems:

## 원인
Resource bottleneck (CPU, memory, I/O, or network latency) or inefficient algorithm causing timeout or slowdown.

## 해결법
1. Notice the CLI is frozen (sometimes only after minutes of waiting)
2. Kill all Claude processes (`pkill -f claude`)
3. Restart with `claude --resume` or `claude --continue`
4. Manually re-explain what was happening: "Your last response was cut off due to a connection loss. You were editing src/auth.ts and had 3 more files to update. Continue from where you left off."
5. Hope Claude doesn't hallucinate that it already completed the work

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/26729
