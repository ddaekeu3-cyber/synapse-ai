---
layout: solution
title: "Token usage statistics returns 0 for non-OpenAI providers since 2026.3.12"
category: token-cost
source: https://github.com/openclaw/openclaw/issues/47421
description: "Starting from OpenClaw 2026.3.12, token usage statistics (input/output tokens) always return 0 for non-OpenAI providers like Bailian (Kimi), even though"
---

# Token usage statistics returns 0 for non-OpenAI providers since 2026.3.12

## 증상
Starting from OpenClaw 2026.3.12, token usage statistics (input/output tokens) always return 0 for non-OpenAI providers like Bailian (Kimi), even though the API correctly returns usage data.

## 원인
Excessive token consumption from repeated failed attempts, large context windows, or inefficient prompt construction. 카테고리: token-cost.

## 해결법
Add `compat.supportsUsageInStreaming: true` to each model configuration:

```json
{
  "id": "kimi-k2.5",
  "name": "kimi-k2.5",
  "compat": {
    "supportsUsageInStreaming": true
  }
}
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/47421
