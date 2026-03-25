---
layout: solution
title: "ACP mode='session' + thread=true fails on Telegram: 'Thread bindings do not support ACP thread spawn'"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/49592
---

# ACP mode="session" + thread=true fails on Telegram: "Thread bindings do not support ACP thread spawn"

## 증상
When using `sessions_spawn` with `runtime: "acp"`, `mode: "session"`, and `thread: true` from a Telegram channel, the call fails with:

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Using `acpx` CLI directly via `exec` to drive persistent Claude Code sessions:

```bash
acpx claude sessions new --name my-session
acpx claude -s my-session "prompt here"
```

This works but loses the lifecycle management (announce, auto-archive, depth tracking) that ACP runtime provides.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/49592
