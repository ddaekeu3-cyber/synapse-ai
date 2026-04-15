---
layout: solution
title: "Add ContextThreshold hook event for proactive memory management"
category: config
source: https://github.com/anthropics/claude-code/issues/38524
description: "PreCompact fires at compaction time — often too late to do anything meaningful. Users need a warning at a configurable threshold (e.g., 80%) so agents can"
---

# Add ContextThreshold hook event for proactive memory management

## 증상
PreCompact fires **at compaction time** — often too late to do anything meaningful. Users need a warning at a configurable threshold (e.g., 80%) so agents can proactively flush memory, wrap up tasks, or create handoff notes while there's still room in the context.

## 원인
Environment variable, configuration file, or initialization parameter missing, malformed, or incorrectly scoped.

## 해결법
There is no reliable workaround. PostToolUse hooks can check transcript file size as a rough proxy, but this is imprecise and doesn't correlate directly with token count. The `context_window.used_percentage` field in the statusline JSON is the closest available signal, but it's only accessible to the statusline script, not to hooks.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/38524
