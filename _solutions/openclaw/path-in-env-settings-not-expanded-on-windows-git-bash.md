---
layout: solution
title: "${PATH} in env settings not expanded on Windows (Git Bash)"
category: openclaw
source: https://github.com/anthropics/claude-code/issues/38385
---

# ${PATH} in env settings not expanded on Windows (Git Bash)

## 증상
The `env.PATH` setting in `~/.claude/settings.json` does not expand `${PATH}` — it is treated as a **literal string**. This means subprocesses spawned by Claude Code (e.g., the `/plugin` marketplace cloner) cannot find executables that are on the Windows system/user PATH.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
List all needed directories explicitly in `env.PATH` instead of relying on `${PATH}` expansion:

```json
"env": {
  "PATH": "/c/Users/me/.bun/bin:/c/Program Files/Git/cmd:/c/Program Files/nodejs:${PATH}"
}
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/38385
