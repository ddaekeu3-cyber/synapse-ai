---
layout: solution
title: "[Feature]: feat: preemptive task interruption via in-channel message"
category: tool-failure
source: https://github.com/openclaw/openclaw/issues/48778
---

# [Feature]: feat: preemptive task interruption via in-channel message

## 증상
This issue proposes a preemptive interrupt mechanism to address the lack of real-time task cancellation for Agents mid-execution (e.g., during tool calls). Currently, interrupt commands (e.g., "stop", "cancel") are queued and only processed after the task completes, making them ineffective for long-running operations. The solution monitors the message channel during execution, immediately aborting

## 원인
보고된 버그/문제. 카테고리: tool-failure.

## 해결법
monitors the message channel during execution, immediately aborting the task upon detecting an authorized interrupt signal (e.g., /stop or keywords) and returning a "cancelled" status.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/48778
