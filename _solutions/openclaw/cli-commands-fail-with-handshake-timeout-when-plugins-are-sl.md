---
layout: solution
title: "CLI commands fail with handshake timeout when plugins are slow to load"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/49118
---

# CLI commands fail with handshake timeout when plugins are slow to load

## 증상
`openclaw cron add`, `openclaw cron list`, `openclaw devices list`, and other CLI commands that connect to the Gateway via WebSocket consistently fail with `connect challenge timeout` when non-bundled plugins (e.g. `chorus-openclaw-plugin`) are auto-discovered and loaded.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Setting `OPENCLAW_SKIP_PLUGINS=1` allows CLI commands to connect successfully:

```bash
OPENCLAW_SKIP_PLUGINS=1 openclaw cron list   # works
OPENCLAW_SKIP_PLUGINS=1 openclaw cron add ... # works
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/49118
