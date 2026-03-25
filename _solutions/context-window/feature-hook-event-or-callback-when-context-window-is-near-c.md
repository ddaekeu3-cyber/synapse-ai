---
layout: solution
title: "[FEATURE]  Hook event or callback when context window is near capacity"
category: context-window
source: https://github.com/anthropics/claude-code/issues/33698
---

# [FEATURE]  Hook event or callback when context window is near capacity

## 증상
- [x] I have searched [existing requests](https://github.com/anthropics/claude-code/issues?q=is%3Aissue%20label%3Aenhancement) and this feature hasn't been requested yet

## 원인
보고된 버그/문제. 카테고리: context-window.

## 해결법
- Runs on every tool call, adding overhead throughout the session
- By the time the flag is detected and acted on, context may already be critically low
- Relies on Claude reliably reading `CLAUDE.md` instructions when context is already degraded, bad time for instruction-following

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/33698
