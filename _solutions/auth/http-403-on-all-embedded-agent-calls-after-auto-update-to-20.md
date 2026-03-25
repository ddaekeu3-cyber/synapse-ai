---
layout: solution
title: "HTTP 403 on all embedded agent calls after auto-update to 2026.2.24 (pi-ai v0.55.0 requires user:profile scope missing from setup-token)"
category: auth
source: https://github.com/openclaw/openclaw/issues/26384
---

# HTTP 403 on all embedded agent calls after auto-update to 2026.2.24 (pi-ai v0.55.0 requires user:profile scope missing from setup-token)

## 증상
- **OpenClaw version:** 2026.2.24 (auto-updated from 2026.2.23)

## 원인
보고된 버그/문제. 카테고리: auth.

## 해결법
Roll back to OpenClaw 2026.2.23:

```bash
systemctl --user stop openclaw-gateway
npm install -g openclaw@2026.2.23
systemctl --user start openclaw-gateway
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/26384
