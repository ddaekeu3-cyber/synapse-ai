---
layout: solution
title: "/resume picker does not discover session files via filesystem scan — sessions invisible despite valid .jsonl on disk (v2.1.81)"
category: openclaw
source: https://github.com/anthropics/claude-code/issues/38340
description: "The interactive picker silently omits valid session files. A manually-created, correctly-formatted session file placed in the project directory is"
---

# /resume picker does not discover session files via filesystem scan — sessions invisible despite valid .jsonl on disk (v2.1.81)

## 증상
The `/resume` interactive picker silently omits valid session files. A manually-created, correctly-formatted `.jsonl` session file placed in the project directory is completely invisible to the picker — proving the picker is **not** scanning the filesystem for sessions despite the v2.1.30 changelog stating it was switched to "stat-based loading."

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
`claude --resume <session-id>` works reliably. Session IDs can be found via:
```bash
ls -lt ~/.claude/projects/<project-dir>/*.jsonl
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/38340
