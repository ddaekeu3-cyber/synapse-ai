---
layout: solution
title: "Gateway event loop freezes on 2026.3.13 — Slack socket mode events not dispatched (Linux/amd64, 2GB VPS)"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/46327
---

# Gateway event loop freezes on 2026.3.13 — Slack socket mode events not dispatched (Linux/amd64, 2GB VPS)

## 증상
Regression (worked before, now fails)

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Root cause identified by @lnxg33k — the security change "require stable channel and team IDs for allowlist routing by default" silently breaks Slack channel configs that use channel names instead of IDs. No error is logged; events are simply never dispatched.
Fix: Replace channel names with channel IDs in channels.slack.channels:

```
// Before (broken on 2026.3.12+)
"channels": { "#my-channel": { "allow": true } }

// After (works)
"channels": { "C0XXXXXXXX": { "allow": true } }
```

This is a breaking change that was not documented as such in the release notes.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/46327
