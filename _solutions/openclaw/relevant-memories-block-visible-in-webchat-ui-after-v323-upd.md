---
layout: solution
title: "<relevant-memories> block visible in webchat UI after v3.23 update"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/53696
description: "After updating to v3.23.2, the block (injected by memory-lancedb-pro auto-recall) is now visible in the webchat transcript. This block should be internal"
---

# <relevant-memories> block visible in webchat UI after v3.23 update

## 증상
After updating to v3.23.2, the `<relevant-memories>` block (injected by memory-lancedb-pro auto-recall) is now visible in the webchat transcript. This block should be internal context only and was not visible before the v3.23 update.

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
Disable autoRecall in memory-lancedb-pro config:
```json
{
  "plugins": {
    "entries": {
      "memory-lancedb-pro": {
        "config": {
          "autoRecall": false
        }
      }
    }
  }
}
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/53696
