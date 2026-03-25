---
layout: solution
title: "Session lock timeout causes channel handler failures during long operations"
category: tool-failure
source: https://github.com/openclaw/openclaw/issues/3092
---

# Session lock timeout causes channel handler failures during long operations

## 증상
Channel handlers (especially Telegram) frequently fail with lock timeout when the agent is processing any long operation (exec commands, HTTP requests, tool calls, etc):

## 원인
보고된 버그/문제. 카테고리: tool-failure.

## 해결법
Cron job to clean stale locks every 2 minutes:
```bash
*/2 * * * * find /root/.clawdbot/agents/main/sessions -name "*.lock" -mmin +2 -delete
```

This helps recover from stuck locks but doesn't prevent the failures.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/3092
