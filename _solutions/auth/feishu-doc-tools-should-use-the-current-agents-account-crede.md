---
layout: solution
title: "Feishu doc tools should use the current agent's account credentials [Critical]"
category: auth
source: https://github.com/openclaw/openclaw/issues/44975
description: "Behavior bug (incorrect output/state without"
---

# Feishu doc tools should use the current agent's account credentials [Critical]

## 증상
Behavior bug (incorrect output/state without crash)

## 원인
GitHub Issue #44975에서 보고된 버그. 해결법이 이슈에 포함됨.

## 해결법
Option 1: Pass agent context to tool execution

Modify the tool execution to receive current agent's `accountId`:
```typescript
async execute(_toolCallId, params, context) {
  const accountId = context?.agentId; // or similar
  const account = resolveFeishuAccount({ cfg: api.config, accountId });
  const client = createFeishuClient(account);
  // ...
}
```

Option 2: Add accountId parameter to tool

Allow explicit account selection in tool parameters:
```typescript
{
  "action": "create",
  "tit

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/44975
