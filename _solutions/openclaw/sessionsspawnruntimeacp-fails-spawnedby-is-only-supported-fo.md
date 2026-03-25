---
layout: solution
title: "sessions_spawn(runtime='acp') fails: 'spawnedBy is only supported for subagent:* sessions'"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/43544
---

# sessions_spawn(runtime="acp") fails: "spawnedBy is only supported for subagent:* sessions"

## 증상
`sessions_spawn(runtime="acp", agentId="claude", ...)` consistently fails with:

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Use Claude Code CLI directly:
```bash
cd <repo> && claude --permission-mode bypassPermissions --print "<task>"
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/43544
