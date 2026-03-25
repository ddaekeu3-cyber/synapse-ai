---
layout: solution
title: "Spawned agents: bypassPermissions ineffective, worktree data loss, plan mode loop"
category: concurrency
source: https://github.com/anthropics/claude-code/issues/29110
---

# Spawned agents: bypassPermissions ineffective, worktree data loss, plan mode loop

## 증상
- Claude Code CLI (latest as of 2026-02-26)

## 원인
보고된 버그/문제. 카테고리: concurrency.

## 해결법
(spawning without worktree isolation or plan mode, and with `bypassPermissions`) also failed for Write/Edit, suggesting `bypassPermissions` is ineffective regardless of worktree usage.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/29110
