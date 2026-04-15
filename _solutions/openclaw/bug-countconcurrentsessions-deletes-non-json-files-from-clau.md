---
layout: solution
title: "Bug: countConcurrentSessions() deletes non-.json files from ~/.claude/sessions/"
category: openclaw
source: https://github.com/anthropics/claude-code/issues/38528
description: "The function in the CLI scans and deletes files belonging to \"dead processes\". However, it incorrectly treats non- files (e.g., files from plugins) as PID"
---

# Bug: countConcurrentSessions() deletes non-.json files from ~/.claude/sessions/

## 증상
The `countConcurrentSessions()` function in the CLI scans `~/.claude/sessions/` and deletes files belonging to "dead processes". However, it incorrectly treats non-`.json` files (e.g., `.tmp` files from plugins) as PID files, parses the year from date-prefixed filenames as a PID, and deletes them.

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
The ECC plugin has submitted a PR to move its session storage to `~/.claude/ecc-sessions/` to avoid this directory entirely: https://github.com/affaan-m/everything-claude-code/pull/899

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/38528
