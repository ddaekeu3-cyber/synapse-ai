---
layout: solution
title: "sessions_send from isolated cron jobs always times out - no message delivery"
category: tool-failure
source: https://github.com/anthropics/claude-code/issues/37830
---

# sessions_send from isolated cron jobs always times out - no message delivery

## 증상
`sessions_send` called from isolated cron job sessions consistently times out, regardless of model used. The tool call initiates but never completes within reasonable timeouts.

## 원인
보고된 버그/문제. 카테고리: tool-failure.

## 해결법
File-based escalation (write to disk, main session checks on heartbeat) works but loses the real-time notification benefit.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/37830
