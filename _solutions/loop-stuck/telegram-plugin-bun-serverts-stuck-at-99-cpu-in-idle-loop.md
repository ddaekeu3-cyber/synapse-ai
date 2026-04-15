---
layout: solution
title: "Telegram plugin: bun server.ts stuck at 99% CPU in idle loop"
category: loop-stuck
source: https://github.com/anthropics/claude-code/issues/38092
description: "The official Telegram plugin () spawns a process that gets stuck in a CPU-burning loop at 99% CPU usage, even when"
---

# Telegram plugin: bun server.ts stuck at 99% CPU in idle loop

## 증상
The official Telegram plugin (`telegram@claude-plugins-official`) spawns a `bun server.ts` process that gets stuck in a CPU-burning loop at **99% CPU** usage, even when idle.

## 원인
Agent entered a retry or decision loop without an exit condition, consuming tokens indefinitely without making progress. 카테고리: loop-stuck.

## 해결법
Force kill the process and disable the plugin:
```bash
kill -9 <pid>
```
Then set `"telegram@claude-plugins-official": false` in `~/.claude/settings.json`.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/38092
