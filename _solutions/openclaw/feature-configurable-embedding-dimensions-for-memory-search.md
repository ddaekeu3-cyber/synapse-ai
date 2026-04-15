---
layout: solution
title: "[Feature]: Configurable Embedding Dimensions for Memory Search"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/17424
description: "Is your feature request related to a problem? Please"
---

# [Feature]: Configurable Embedding Dimensions for Memory Search

## 증상
**Is your feature request related to a problem? Please describe.**

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
you'd like**
Add support for the `dimensions` parameter in `agents.defaults.memorySearch` (and per-agent overrides). This should be passed through to the embedding API call.

Example configuration:
```json
{
  "agents": {
    "defaults": {
      "memorySearch": {
        "model": "openai/text-embedding-3-large",
        "dimensions": 1024
      }
    }
  }
}
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/17424
