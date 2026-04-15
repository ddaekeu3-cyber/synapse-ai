---
layout: solution
title: "Gateway crashes on startup with bad hook entries — graceful error handling needed"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/51266
description: "If a user adds a hook entry to that points to a non-existent path, the gateway crashes on startup with an unhandled error. The error message doesn't"
---

# Gateway crashes on startup with bad hook entries — graceful error handling needed

## 증상
If a user adds a hook entry to `openclaw.json` that points to a non-existent path, the gateway crashes on startup with an unhandled `MODULE_NOT_FOUND` error. The error message doesn't indicate which hook entry is bad or how to fix it.

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
We've published repair scripts in our workspace repo that catch this before the gateway loads:
- `scripts/validate-hooks.sh` — pre-startup validation
- `scripts/fix-openclaw-config.sh` — auto-repair with backup

These work but ideally the gateway itself would be resilient.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/51266
