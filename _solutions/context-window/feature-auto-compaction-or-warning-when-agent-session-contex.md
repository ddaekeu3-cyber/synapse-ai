---
layout: solution
title: "[Feature]: Auto-compaction or warning when agent session context exceeds threshold"
category: context-window
source: https://github.com/openclaw/openclaw/issues/43603
description: "Agent sessions gradually bloat in token usage over extended conversations. When a session reaches 70-80%+ of the context window, the agent becomes"
---

# [Feature]: Auto-compaction or warning when agent session context exceeds threshold

## 증상
Agent sessions gradually bloat in token usage over extended conversations. When a session reaches 70-80%+ of the context window, the agent becomes sluggish, starts timing out, or freezes entirely. There's no automatic compaction and no warning to the user or operator.

## 원인
Input exceeded the model's maximum context length, causing truncation or a refusal to process the full request. 카테고리: context-window.

## 해결법
Manual monitoring via `openclaw sessions --agent <id> --json` and manually resetting sessions by replacing the .jsonl transcript file. This is fragile and loses conversation history.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/43603
