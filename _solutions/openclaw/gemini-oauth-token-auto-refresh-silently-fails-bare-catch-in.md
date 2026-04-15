---
layout: solution
title: "Gemini OAuth token auto-refresh silently fails — bare catch {} in dispatch swallows refresh errors"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/42541
description: "Gemini CLI OAuth tokens expire after ~60 minutes and are never auto-refreshed, despite valid and being present in . The gateway silently falls into an"
---

# Gemini OAuth token auto-refresh silently fails — bare catch {} in dispatch swallows refresh errors

## 증상
Gemini CLI OAuth tokens expire after ~60 minutes and are never auto-refreshed, despite valid `refresh_token` and `projectId` being present in `auth-profiles.json`. The gateway silently falls into an unauthenticated state with **zero log output** about the failed refresh.

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
External cron job that proactively refreshes the token every 45 minutes before expiry:

```bash
*/45 * * * * /path/to/refresh-gemini-tokens.sh >> /path/to/token-refresh.log 2>&1
```

Script calls `https://oauth2.googleapis.com/token` directly and writes the new `access` + `expires` into all agent `auth-profiles.json` files.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/42541
