---
layout: solution
title: "Multiple parallel foreground agents freeze after ~15-30 min, unblock on ctrl+b (move to background)"
category: tool-failure
source: https://github.com/anthropics/claude-code/issues/36195
---

# Multiple parallel foreground agents freeze after ~15-30 min, unblock on ctrl+b (move to background)

## 증상
When launching 3-4+ parallel Agent tool calls (foreground, no `run_in_background`), all agents freeze after approximately 15-30 minutes. Token consumption stops, tool calls show as active but make no progress (e.g., stuck on "Web Search", "Fetch", "Bash").

## 원인
보고된 버그/문제. 카테고리: tool-failure.

## 해결법
Press ctrl+b to move agents to background when they freeze. They resume immediately.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/36195
