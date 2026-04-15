---
layout: solution
title: "Ollama tool call arguments serialized as string instead of object — breaks tool loop"
category: tool-failure
source: https://github.com/openclaw/openclaw/issues/50689
description: "When an Ollama model returns a tool call, OpenClaw stores as a JSON string (OpenAI format). When this assistant message is later sent back to Ollama as"
---

# Ollama tool call arguments serialized as string instead of object — breaks tool loop

## 증상
When an Ollama model returns a tool call, OpenClaw stores `arguments` as a JSON string (OpenAI format). When this assistant message is later sent back to Ollama as conversation context, `extractToolCalls()` passes `arguments` as-is (string). Ollama expects an **object** and fails with `"Value looks like object, but can't find closing '}' symbol"`.

## 원인
Tool or plugin call failed due to schema mismatch, missing parameter, permission error, or upstream API change. 카테고리: tool-failure.

## 해결법
In `extractToolCalls()`, deserialize string arguments before passing to Ollama:

```javascript
const ensureObj = (v) => { if (typeof v === "string") try { return JSON.parse(v); } catch {} return v ?? {}; };
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/50689
