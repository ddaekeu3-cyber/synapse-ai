---
layout: solution
title: "Read tool errors are silently swallowed in TUI — no error message shown"
category: tool-failure
source: https://github.com/anthropics/claude-code/issues/23699
---

# Read tool errors are silently swallowed in TUI — no error message shown

## 증상
When the Read tool fails, the TUI shows only a pink dot with the tool call — no error message is displayed. The error details are available in the tool result (the model can see them), but the user cannot.

## 원인
보고된 버그/문제. 카테고리: tool-failure.

## 해결법
it.

✍️ **Author**: Claude Code with @carrotRakko (AI-written, human-approved)

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/23699
