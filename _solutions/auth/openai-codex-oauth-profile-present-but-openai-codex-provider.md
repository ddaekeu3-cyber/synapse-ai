---
layout: solution
title: "openai-codex OAuth profile present but openai-codex provider not injected into models.json"
category: auth
source: https://github.com/openclaw/openclaw/issues/40364
description: "Regression (worked before, now"
---

# openai-codex OAuth profile present but openai-codex provider not injected into models.json

## 증상
Regression (worked before, now fails)

## 원인
Authentication credential mismatch, expiry, or permission scope gap between the requesting agent and the target API.

## 해결법
Manually adding to `openclaw.json` resolves it:

```json
{
  "models": {
    "providers": {
      "openai-codex": {
        "baseUrl": "https://chatgpt.com/backend-api",
        "api": "openai-codex-responses",
        "models": []
      }
    }
  }
}
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/40364
