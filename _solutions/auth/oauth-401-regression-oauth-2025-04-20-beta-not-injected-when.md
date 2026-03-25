---
layout: solution
title: "OAuth 401 regression: oauth-2025-04-20 beta not injected when context1m set via model headers"
category: auth
source: https://github.com/openclaw/openclaw/issues/41444
---

# OAuth 401 regression: oauth-2025-04-20 beta not injected when context1m set via model headers

## 증상
When the Anthropic API key is an OAuth token (`sk-ant-oat-*`), requests fail with HTTP 401 if `context-1m-2025-08-07` is configured via **model-level headers** rather than via **agent extra params** (`context1m: true`). The `oauth-2025-04-20` beta header is never injected, causing Anthropic to reject the Bearer auth.

## 원인
보고된 버그/문제. 카테고리: auth.

## 해결법
Move `context-1m` from model-level headers to agent extra params:

```json
// Remove from models.providers.anthropic.models[].headers
// Add to agents.defaults.models:
"anthropic/claude-opus-4-6": {
    "params": { "context1m": true, "cacheRetention": "short" }
}
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/41444
