---
layout: solution
title: "CLI errors after /clear when previous cwd was a removed directory"
category: general
source: https://github.com/anthropics/claude-code/issues/37257
---

# CLI errors after /clear when previous cwd was a removed directory

## 증상
After `/clear`, Claude Code tries to restore the working directory from the previous session context. If that directory has been removed (e.g., a git worktree that was cleaned up), the CLI throws a hard error instead of falling back gracefully.

## 원인
보고된 버그/문제. 카테고리: general.

## 해결법
Start a new `claude` session instead of using `/clear` when the worktree has been removed.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/37257
