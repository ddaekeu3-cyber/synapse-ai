---
layout: solution
title: "OpenAI Codex OAuth in 2026.3.13 does not honor env proxy during code-to-token exchange"
category: auth
source: https://github.com/openclaw/openclaw/issues/51569
description: "Regression (worked before, now"
---

# OpenAI Codex OAuth in 2026.3.13 does not honor env proxy during code-to-token exchange

## 증상
Regression (worked before, now fails)

## 원인
GitHub Issue #51569에서 보고된 버그. 해결법이 이슈에 포함됨.

## 해결법
import a proxy-init shim before the OpenAI OAuth flow so the global undici dispatcher becomes `EnvHttpProxyAgent`.

This looks like a regression caused by the move from `@mariozechner/pi-ai` to `@mariozechner/pi-ai/oauth` without preserving the previous proxy-init side effect.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/51569
