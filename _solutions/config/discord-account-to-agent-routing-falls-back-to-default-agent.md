---
layout: solution
title: "Discord account-to-agent routing falls back to default agent instead of matching by account name"
category: config
source: https://github.com/openclaw/openclaw/issues/39428
description: "When a Discord channel account name matches an agent ID, inbound messages should automatically route to that agent. Instead, they fall back to the default"
---

# Discord account-to-agent routing falls back to default agent instead of matching by account name

## 증상
When a Discord channel account name matches an agent ID, inbound messages should automatically route to that agent. Instead, they fall back to the default (main) agent, requiring explicit `bindings` config to fix.

## 원인
Environment variable, configuration file, or initialization parameter missing, malformed, or incorrectly scoped.

## 해결법
Adding explicit bindings resolves the issue:

```json
{
  "bindings": [
    { "match": { "channel": "discord", "accountId": "agentA" }, "agentId": "agentA" },
    { "match": { "channel": "discord", "accountId": "agentB" }, "agentId": "agentB" }
  ]
}
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/39428
