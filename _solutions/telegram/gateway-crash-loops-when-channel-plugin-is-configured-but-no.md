---
layout: solution
title: "Gateway crash-loops when channel plugin is configured but not installed"
category: telegram
---

# Gateway crash-loops when channel plugin is configured but not installed

## 증상
- **OpenClaw version:** 2026.3.23-beta.1 (also affects 2026.3.22)

에러 메시지:
` references an unknown channel id, treats it as a fatal error, and exits 1. Systemd restarts it, same result, crash-loop.

`

## 원인
원본 이슈에서 확인 필요. GitHub Issue #53311 참조.

## 해결법
Downgrade `channels.<id>: unknown channel id` from a fatal config validation error to a startup warning. Skip the unconfigured channel and continue with the rest. This is consistent with how `plugins.entries.<id>: plugin not found` is already handled (logged as "stale config entry ignored").

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: telegram
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/53311
