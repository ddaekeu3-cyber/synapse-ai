---
layout: solution
title: "OAuth token refresh race condition kills parent session during parallel agent spawns"
category: concurrency
source: https://github.com/anthropics/claude-code/issues/37996
description: "When spawning 3-4 parallel agents (via tool with ), the parent/lead session intermittently loses authentication mid-operation with \"Not logged in · Please"
---

# OAuth token refresh race condition kills parent session during parallel agent spawns

## 증상
When spawning 3-4 parallel agents (via `Agent` tool with `isolation: "worktree"`), the parent/lead session intermittently loses authentication mid-operation with "Not logged in · Please run /login" followed by `API Error: 401 {"type":"error","error":{"type":"authentication_error","message":"OAuth authentication is currently not supported."}}`.

## 원인
Race condition or deadlock from multiple concurrent operations targeting the same shared resource without proper locking.

## 해결법
- Cap concurrent agents at 3 (instead of 4)
- Stagger agent spawns by 30 seconds (gives 4x margin over the 7.5s lock window)
- When it happens: `/login` in the dead session, or start a new terminal

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/37996
