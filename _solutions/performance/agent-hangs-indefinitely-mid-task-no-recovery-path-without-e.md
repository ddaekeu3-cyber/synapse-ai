---
layout: solution
title: "Agent hangs indefinitely mid-task — no recovery path without Esc (blocks remote/headless use)"
category: performance
source: https://github.com/anthropics/claude-code/issues/28482
---

# Agent hangs indefinitely mid-task — no recovery path without Esc (blocks remote/headless use)

## 증상
Claude Code frequently hangs indefinitely mid-task — it stops producing output and makes no further tool calls, but doesn't return control to the user. The session appears stuck with no timeout or automatic recovery.

## 원인
보고된 버그/문제. 카테고리: performance.

## 해결법
is to press **Esc** to interrupt the agent and re-prompt. But this makes **remote control / headless use non-viable**, since there is no equivalent of pressing Esc programmatically or via the SDK.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/28482
