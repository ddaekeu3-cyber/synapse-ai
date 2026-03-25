---
layout: solution
title: "Mobile (iOS): no streaming output visibility for long-running Bash commands"
category: tool-failure
source: https://github.com/anthropics/claude-code/issues/38260
---

# Mobile (iOS): no streaming output visibility for long-running Bash commands

## 증상
When running long-running `Bash` tool calls (container builds, package installs, cargo builds, etc.) through Claude Code on the iOS app, there is no streaming output or progress visibility. The tool call card appears but shows nothing until the command completes.

## 원인
보고된 버그/문제. 카테고리: tool-failure.

## 해결법
Ask Claude to use `run_in_background: true` and redirect output:

```bash
podman build -t myimage . > /tmp/build.log 2>&1
```

This keeps the conversation responsive but provides no in-app visibility into the log. A desktop user can tail it; an iPhone user cannot.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/38260
