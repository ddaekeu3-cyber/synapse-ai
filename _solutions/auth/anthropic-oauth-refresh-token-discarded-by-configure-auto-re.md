---
layout: solution
title: "Anthropic OAuth refresh token discarded by configure; auto-refresh always fails, requires manual re-auth every ~8h"
category: auth
source: https://github.com/openclaw/openclaw/issues/34117
description: "- Provider: Anthropic"
---

# Anthropic OAuth refresh token discarded by configure; auto-refresh always fails, requires manual re-auth every ~8h

## 증상
- **Provider:** Anthropic (claude-sonnet-4-6)

## 원인
Authentication credential mismatch, expiry, or permission scope gap between the requesting agent and the target API.

## 해결법
Manually edit `~/.openclaw/agents/main/agent/auth-profiles.json` after every configure run:
```json
"anthropic:claude-cli": {
  "type": "oauth",
  "provider": "anthropic",
  "access": "sk-ant-oat01-...",
  "expires": <now + 8h in ms>
}
```
And manually remove `auth.order` from `openclaw.json`.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/34117
