---
layout: solution
title: "OpenClaw.app 2026.2.22 corrupts gateway.mode config (double-write bug causing 14h+ crash loop)"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/24724
description: "Version: OpenClaw.app 2026.2.22 (macOS desktop"
---

# OpenClaw.app 2026.2.22 corrupts gateway.mode config (double-write bug causing 14h+ crash loop)

## 증상
**Version:** OpenClaw.app 2026.2.22 (macOS desktop app)

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
Manual edit of `~/.openclaw/openclaw.json`, line 141:

```diff
- "mode": "remote",
+ "mode": "local",
```

Gateway auto-recovers on next LaunchAgent restart attempt.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/24724
