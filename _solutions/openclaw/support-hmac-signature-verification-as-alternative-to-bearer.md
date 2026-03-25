---
layout: solution
title: "Support HMAC signature verification as alternative to bearer token auth for hooks"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/32250
---

# Support HMAC signature verification as alternative to bearer token auth for hooks

## 증상
External webhooks (GitHub, Mercury, Stripe, etc.) sign payloads with HMAC-SHA256 but cannot add bearer tokens to their requests. The current hooks system requires bearer token authentication, which means external webhook providers cannot directly invoke OpenClaw hooks.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Running a separate webhook gateway (TypeScript/Node.js) on the same machine that:
1. Receives external webhooks on a different port
2. Verifies HMAC signatures
3. Performs the transform logic directly (since it can't forward to OpenClaw hooks without bearer auth)

This works but duplicates routing/transform infrastructure that hooks already provide.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/32250
