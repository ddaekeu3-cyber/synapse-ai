---
layout: solution
title: "google-vertex provider: '<authenticated>' sentinel passed as API key breaks ADC auth"
category: auth
source: https://github.com/openclaw/openclaw/issues/50053
description: "When using the provider with Application Default Credentials (ADC) via a service account, OpenClaw passes the literal string as to the pi-ai stream"
---

# google-vertex provider: "<authenticated>" sentinel passed as API key breaks ADC auth

## 증상
When using the `google-vertex` provider with Application Default Credentials (ADC) via a service account, OpenClaw passes the literal string `"<authenticated>"` as `options.apiKey` to the pi-ai stream handler, which then tries to use it as an actual API key instead of falling through to ADC.

## 원인
Authentication credential mismatch, expiry, or permission scope gap between the requesting agent and the target API.

## 해결법
Two changes are needed:

**1. Set `auth: "oauth"` on the google-vertex provider in openclaw.json:**
```json
{
  "google-vertex": {
    "models": [...],
    "baseUrl": "https://aiplatform.googleapis.com",
    "auth": "oauth"
  }
}
```

**2. Patch pi-ai's `resolveApiKey` in `node_modules/@mariozechner/pi-ai/dist/providers/google-vertex.js`:**
```js
// Original:
function resolveApiKey(options) {
    return options?.apiKey || process.env.GOOGLE_CLOUD_API_KEY;
}

// Patched:
function resolveApiKey(options) {
    const _k = options?.apiKey || process.env.GOOGLE_CLOUD_API_KEY;
    return (_k && !_k.s

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/50053
