---
layout: solution
title: "[FEATURE] Add internal reasoning checkpoints in --dangerously-skip-permissions mode"
category: general
source: https://github.com/anthropics/claude-code/issues/32337
---

# [FEATURE] Add internal reasoning checkpoints in --dangerously-skip-permissions mode

## 증상
- [x] I have searched [existing requests](https://github.com/anthropics/claude-code/issues?q=is%3Aissue%20label%3Aenhancement) and this feature hasn't been requested yet

## 원인
보고된 버그/문제. 카테고리: general.

## 해결법
belongs in the agent's execution loop, not in the permission system.

Environment

- Claude Code CLI (latest)
- Used across multiple projects with CLAUDE.md context files
- Observed consistently over 3+ weeks of daily usage
- Comparison done with identical prompts, same codebase, permissions on vs. off

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/32337
