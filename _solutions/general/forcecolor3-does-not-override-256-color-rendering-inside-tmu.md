---
layout: solution
title: "FORCE_COLOR=3 does not override 256-color rendering inside tmux"
category: general
source: https://github.com/anthropics/claude-code/issues/37770
---

# FORCE_COLOR=3 does not override 256-color rendering inside tmux

## 증상
Claude Code renders 256-color escape sequences inside tmux even when `FORCE_COLOR=3` is set. The workaround is unsetting `TMUX` entirely (`TMUX= claude`).

## 원인
보고된 버그/문제. 카테고리: general.

## 해결법
Launch Claude Code with `TMUX=` unset:
```bash
TMUX= claude
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/37770
