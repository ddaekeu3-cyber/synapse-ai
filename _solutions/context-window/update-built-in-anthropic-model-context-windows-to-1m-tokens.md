---
layout: solution
title: "Update built-in Anthropic model context windows to 1M tokens"
category: context-window
source: https://github.com/openclaw/openclaw/issues/47440
---

# Update built-in Anthropic model context windows to 1M tokens

## 증상
Claude Opus 4.5 and Sonnet 4.5 now support 1M token context windows via the Anthropic API, but Clawdbot's built-in model definitions still use the older 200K limit.

## 원인
보고된 버그/문제. 카테고리: context-window.

## 해결법
Users can override via config:
```json
{
  "models": {
    "providers": {
      "anthropic": {
        "baseUrl": "https://api.anthropic.com",
        "auth": "oauth",
        "api": "anthropic-messages",
        "models": [
          {
            "id": "claude-opus-4-5",
            "name": "Claude Opus 4.5",
            "contextWindow": 1000000,
            "maxTokens": 32000,
            "reasoning": true,
            "input": ["text", "image"]
          }
        ]
      }
    }
  }
}
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/47440
