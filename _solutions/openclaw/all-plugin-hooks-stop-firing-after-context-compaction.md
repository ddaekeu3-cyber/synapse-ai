---
layout: solution
title: "All plugin hooks stop firing after context compaction"
category: openclaw
source: https://github.com/anthropics/claude-code/issues/25655
---

# All plugin hooks stop firing after context compaction

## 증상
After context compaction (triggered when a conversation exceeds the context window limit), **all** plugin hooks stop firing for the remainder of the session. This affects every hook type: `PostToolUse`, `SessionStart`, `Stop` — not just a single event type.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Starting a new session (`/clear` or new `claude` invocation) re-registers hooks via `SessionStart` and restores normal hook execution.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/25655
