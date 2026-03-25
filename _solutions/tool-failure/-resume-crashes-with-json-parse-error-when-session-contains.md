---
layout: solution
title: "--resume crashes with JSON Parse error when session contains Agent Teams artifacts (queue-operation, SendMessage, Agent)"
category: tool-failure
source: https://github.com/anthropics/claude-code/issues/38379
---

# --resume crashes with JSON Parse error when session contains Agent Teams artifacts (queue-operation, SendMessage, Agent)

## 증상
`claude --resume <session-id>` crashes with a JSON Parse error when the session JSONL file contains Agent Teams artifacts (`queue-operation` entries, `SendMessage` tool calls, `Agent` tool calls with team tasks).

## 원인
보고된 버그/문제. 카테고리: tool-failure.

## 해결법
Manually cleaning the session JSONL file by removing agent team artifacts makes the session resumable:

```bash

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/38379
