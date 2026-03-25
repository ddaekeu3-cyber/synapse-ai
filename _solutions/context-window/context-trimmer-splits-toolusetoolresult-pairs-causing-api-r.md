---
layout: solution
title: "Context trimmer splits tool_use/tool_result pairs, causing API rejection"
category: context-window
source: https://github.com/openclaw/openclaw/issues/52024
---

# Context trimmer splits tool_use/tool_result pairs, causing API rejection

## 증상
When a session transcript grows large enough to require context trimming (to fit within the model's context window), the trimmer can remove an assistant message containing a `tool_use` block while keeping the subsequent user message containing the corresponding `tool_result` block. This produces an orphaned `tool_result` that the Anthropic API rejects with:

## 원인
보고된 버그/문제. 카테고리: context-window.

## 해결법
Gateway restart clears the session's in-memory state. The session continues from the last valid point in the transcript, but the user experience is disruptive (mid-response interruption, context loss).

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/52024
