---
layout: solution
title: "context1m beta header incorrectly skipped for OAuth tokens (usage-based billing)"
category: auth
source: https://github.com/openclaw/openclaw/issues/27846
---

# context1m beta header incorrectly skipped for OAuth tokens (usage-based billing)

## 증상
OpenClaw skips the `context-1m-2025-08-07` Anthropic beta header when the auth token is an OAuth token (`sk-ant-oat-*`). The code in `createAnthropicBetaHeadersWrapper` filters out the 1M beta from `effectiveBetas` and logs:

## 원인
보고된 버그/문제. 카테고리: auth.

## 해결법
Patching the 4 dist files locally to always use `const effectiveBetas = betas;` works but gets overwritten on updates.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/27846
