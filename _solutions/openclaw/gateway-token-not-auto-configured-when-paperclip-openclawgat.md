---
layout: solution
title: "Gateway token not auto-configured when Paperclip openclaw_gateway agents connect"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/44493
description: "When using Paperclip (paperclipai/paperclip) to orchestrate OpenClaw agents via the adapter, the gateway WebSocket connection fails"
---

# Gateway token not auto-configured when Paperclip openclaw_gateway agents connect

## 증상
When using **Paperclip** (paperclipai/paperclip) to orchestrate OpenClaw agents via the `openclaw_gateway` adapter, the gateway WebSocket connection fails with:

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
Manually update the agent's `adapter_config` in PostgreSQL:

```sql
UPDATE agents 
SET adapter_config = adapter_config || '{"headers": {"x-openclaw-token": "<gateway-token>"}}'::jsonb
WHERE adapter_type = 'openclaw_gateway';
```

Or use the tokenized URL for the Control UI: `http://127.0.0.1:18789/#token=<gateway-token>`

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/44493
