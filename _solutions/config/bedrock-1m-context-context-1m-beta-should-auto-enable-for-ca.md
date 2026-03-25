---
layout: solution
title: "Bedrock: 1M context (context-1m beta) should auto-enable for capable models"
category: config
source: https://github.com/anthropics/claude-code/issues/32673
---

# Bedrock: 1M context (context-1m beta) should auto-enable for capable models

## 증상
Opus 4.6 and Sonnet 4.6 natively support 1M token context windows, but on Bedrock this requires the `context-1m-2025-08-07` beta flag in the `anthropic_beta` request body field. Currently, users must manually append `[1m]` to every model ID environment variable to enable this. Claude Code should auto-enable 1M context for models that support it when using Bedrock.

## 원인
보고된 버그/문제. 카테고리: config.

## 해결법
Users must add `[1m]` suffix to **every** model ID config:

```json
{
  "env": {
    "ANTHROPIC_MODEL": "global.anthropic.claude-opus-4-6-v1[1m]",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "global.anthropic.claude-opus-4-6-v1[1m]",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "global.anthropic.claude-sonnet-4-6-v1[1m]",
    "ANTHROPIC_SMALL_FAST_MODEL": "global.anthropic.claude-haiku-4-5-20251001-v1:0"
  }
}
```

Missing `[1m]` on any one of these variables silently limits that model to 200k context, with no warning or error — the request simply gets truncated.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/32673
