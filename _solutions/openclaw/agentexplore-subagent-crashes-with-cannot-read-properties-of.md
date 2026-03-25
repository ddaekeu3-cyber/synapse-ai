---
layout: solution
title: "Agent/Explore subagent crashes with 'Cannot read properties of undefined (reading 'input_tokens')'"
category: openclaw
source: https://github.com/anthropics/claude-code/issues/34899
---

# Agent/Explore subagent crashes with "Cannot read properties of undefined (reading 'input_tokens')"

## 증상
When using the Agent tool (with `subagent_type: Explore` or `general-purpose`), the call consistently fails with:

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Avoid using Agent/Explore subagents entirely. Use Glob, Grep, and Read tools directly for codebase searches. This works but loses the parallelism and autonomy benefits of subagents.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/34899
