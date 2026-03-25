---
layout: solution
title: "Context corruption exposes raw API errors to chat surface"
category: tool-failure
source: https://github.com/openclaw/openclaw/issues/11038
---

# Context corruption exposes raw API errors to chat surface

## 증상
When a session transcript becomes corrupted (orphaned `tool_result` without matching `tool_use`), the raw Anthropic API error is delivered directly to the chat surface instead of being handled gracefully.

## 원인
보고된 버그/문제. 카테고리: tool-failure.

## 해결법
Manual fix: delete/move the corrupted session transcript. Next message starts fresh.

```bash
mv ~/.openclaw/agents/main/sessions/<session-id>.jsonl ~/.Trash/
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/11038
