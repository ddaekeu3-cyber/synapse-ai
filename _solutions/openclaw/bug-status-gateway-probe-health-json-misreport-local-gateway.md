---
layout: solution
title: "Bug: `status` / `gateway probe` / `health --json` misreport local gateway + Telegram state on 2026.3.13"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/49758
description: "On a local loopback gateway setup running OpenClaw , several built-in CLI diagnostics misreport gateway and Telegram health even while the system is"
---

# Bug: `status` / `gateway probe` / `health --json` misreport local gateway + Telegram state on 2026.3.13

## 증상
On a local loopback gateway setup running OpenClaw `2026.3.13`, several built-in CLI diagnostics misreport gateway and Telegram health even while the system is operational.

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
Treat these as source of truth for now:
- `openclaw channels status --probe`
- explicit token-based gateway calls

Do not trust `status`, `gateway probe`, or `health --json` for gateway/Telegram truth on this host/build.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/49758
