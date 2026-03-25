---
layout: solution
title: "Worktrees should share the same project directory as the main repo"
category: config
source: https://github.com/anthropics/claude-code/issues/34437
---

# Worktrees should share the same project directory as the main repo

## 증상
When using git worktrees (either via `claude --worktree` or manually created), Claude Code creates a **separate project directory** in `~/.claude/projects/` for each worktree path. This splits conversation history, auto-memory, and project-level settings across multiple isolated directories — even though the worktrees are branches of the same repository.

## 원인
보고된 버그/문제. 카테고리: config.

## 해결법
Manually moving `.jsonl` files, UUID session directories, and memory files from worktree project directories into the main project directory, then deleting the empty source directories. This works but needs to be repeated after every worktree session.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/34437
