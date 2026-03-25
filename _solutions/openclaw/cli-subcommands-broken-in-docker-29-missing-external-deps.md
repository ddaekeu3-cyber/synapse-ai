---
layout: solution
title: "CLI subcommands broken in Docker: ~29 missing external deps"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/48875
---

# CLI subcommands broken in Docker: ~29 missing external deps

## 증상
1. `docker pull ghcr.io/openclaw/openclaw:main`

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Use the Gateway HTTP API instead of CLI:

```bash
curl -X POST http://localhost:18789/v1/chat/completions \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"model":"openclaw:main","messages":[{"role":"user","content":"test"}]}'
```

Requires `gateway.http.endpoints.chatCompletions.enabled: true` in config.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/48875
