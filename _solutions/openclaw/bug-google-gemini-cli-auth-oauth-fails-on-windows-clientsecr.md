---
layout: solution
title: "Bug: google-gemini-cli-auth OAuth fails on Windows (client_secret missing + loadCodeAssist 400)"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/30403
---

# Bug: google-gemini-cli-auth OAuth fails on Windows (client_secret missing + loadCodeAssist 400)

## 증상
- **OpenClaw version:** 2026.2.26

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Manually set the env vars that `resolveOAuthClientConfig()` checks first (per `oauth.ts` lines 7–10):

```
OPENCLAW_GEMINI_OAUTH_CLIENT_ID=<client_id from oauth2.js>
OPENCLAW_GEMINI_OAUTH_CLIENT_SECRET=<client_secret from oauth2.js>
```

---

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/30403
