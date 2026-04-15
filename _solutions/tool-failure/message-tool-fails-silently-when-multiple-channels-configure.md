---
layout: solution
title: "message tool fails silently when multiple channels configured — no context-aware fallback"
category: tool-failure
source: https://github.com/openclaw/openclaw/issues/19979
description: "When multiple messaging channels are configured (e.g. + ), the tool always fails on first call if the LLM agent omits the parameter — even when the"
---

# message tool fails silently when multiple channels configured — no context-aware fallback

## 증상
When multiple messaging channels are configured (e.g. `telegram` + `whatsapp`), the `message` tool **always fails** on first call if the LLM agent omits the `channel` parameter — even when the conversation context makes the intended channel unambiguous (e.g. replying to a WhatsApp message).

## 원인
Tool or plugin call failed due to schema mismatch, missing parameter, permission error, or upstream API change. 카테고리: tool-failure.

## 해결법
Explicitly document `channel` as mandatory in the agent's workspace `TOOLS.md`:
```
`message` — ALWAYS specify `channel` (required, two active channels).
```

This reduces but doesn't eliminate the issue since the tool schema still marks it as optional.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/19979
