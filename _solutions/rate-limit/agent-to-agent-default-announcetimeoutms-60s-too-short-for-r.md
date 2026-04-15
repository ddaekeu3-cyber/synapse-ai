---
layout: solution
title: "[Agent-to-Agent] Default announceTimeoutMs (60s) too short for reliable agent communication"
category: rate-limit
source: https://github.com/openclaw/openclaw/issues/40863
description: "The default of 60 seconds is too short for reliable agent-to-agent communication, causing to return before the target agent can"
---

# [Agent-to-Agent] Default announceTimeoutMs (60s) too short for reliable agent communication

## 증상
The default `announceTimeoutMs` of 60 seconds is too short for reliable agent-to-agent communication, causing `sessions_send` to return `{status: "timeout"}` before the target agent can respond.

## 원인
API rate limit reached — too many requests within the allowed time window triggered the provider's throttling mechanism. 카테고리: rate-limit.

## 해결법
Manually set in openclaw.json:

```json
{
  "agents": {
    "defaults": {
      "subagents": {
        "announceTimeoutMs": 120000
      }
    }
  }
}
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/40863
