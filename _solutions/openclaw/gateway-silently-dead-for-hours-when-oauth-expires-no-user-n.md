---
layout: solution
title: "Gateway silently dead for hours when OAuth expires — no user notification, no auto-recovery"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/44931
---

# Gateway silently dead for hours when OAuth expires — no user notification, no auto-recovery

## 증상
- **Version:** OpenClaw 2026.3.11 (29dc654)

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
- `anthropic-token-sync.timer` — checks token expiry every 30 min
- Alerts to Telegram when expires < 1 hour
- Auto-syncs credentials from `~/.claude/.credentials.json` to all `auth-profiles.json`

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/44931
