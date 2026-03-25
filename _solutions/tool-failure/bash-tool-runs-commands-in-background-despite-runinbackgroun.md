---
layout: solution
title: "Bash tool runs commands in background despite run_in_background not being set"
category: tool-failure
source: https://github.com/anthropics/claude-code/issues/31518
---

# Bash tool runs commands in background despite run_in_background not being set

## 증상
The Bash tool intermittently runs commands in background (returning a `task_id` and writing output to a temp file) even when `run_in_background` is **not** set to `true` in the tool call.

## 원인
보고된 버그/문제. 카테고리: tool-failure.

## 해결법
None reliable. The behavior is intermittent — the same commands run in foreground in other sessions.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/31518
