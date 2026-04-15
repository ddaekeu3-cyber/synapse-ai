---
layout: solution
title: "Feature request: Expose session_id to the conversation context"
category: config
source: https://github.com/anthropics/claude-code/issues/38390
description: "Claude Code's is available in the JSON piped to statusLine commands and hooks, but is not accessible from within the conversation itself — not as an"
---

# Feature request: Expose session_id to the conversation context

## 증상
Claude Code's `session_id` is available in the JSON piped to statusLine commands and hooks, but is not accessible from within the conversation itself — not as an environment variable, not through any tool, and not in any system context.

## 원인
Environment variable, configuration file, or initialization parameter missing, malformed, or incorrectly scoped.

## 해결법
we implemented for the [ccstatusline](https://github.com/sirmalloc/ccstatusline) task-objective widget:

1. The statusLine command receives `session_id` in its stdin JSON
2. It walks up the process tree (`ps -o ppid=,comm=`) to find the Claude CLI PID
3. It writes the session ID to `~/.cache/ccstatusline/sessions/<claude-pid>`
4. Claude runs `echo $PPID` to get the CLI PID, then reads the file to discover its session ID

This works but is fragile (Unix-only, requires process tree walking) and adds latency to every statusLine render.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/38390
