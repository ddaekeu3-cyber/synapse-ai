---
layout: solution
title: "Crash on worktree session exit leaves terminal in broken state (Windows)"
category: general
source: https://github.com/anthropics/claude-code/issues/31557
---

# Crash on worktree session exit leaves terminal in broken state (Windows)

## 증상
Claude Code crashes when exiting/wrapping up a worktree session, leaving unreadable/garbled text in the terminal. The terminal appears to be left in raw mode or with incomplete ANSI escape sequences.

## 원인
보고된 버그/문제. 카테고리: general.

## 해결법
.

## Environment

- **Claude Code version**: 2.1.63
- **OS**: Windows 11 Pro for Workstations (10.0.26200)
- **Shell**: PowerShell
- **Terminal**: VS Code integrated terminal

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/31557
