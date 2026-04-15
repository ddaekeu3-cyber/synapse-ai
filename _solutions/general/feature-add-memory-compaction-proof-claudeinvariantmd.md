---
layout: solution
title: "[FEATURE] Add memory-compaction-proof ~/.claude/INVARIANT.md"
category: general
source: https://github.com/anthropics/claude-code/issues/34716
description: "- [x] I have searched existing requests and this feature hasn't been requested"
---

# [FEATURE] Add memory-compaction-proof ~/.claude/INVARIANT.md

## 증상
- [x] I have searched [existing requests](https://github.com/anthropics/claude-code/issues?q=is%3Aissue%20label%3Aenhancement) and this feature hasn't been requested yet

## 원인
Agent encountered an unexpected state or unhandled error condition outside the standard error handling path.

## 해결법
alias cc='claude --append-system-prompt "$(cat ~/.claude/invariant-rules.md)"'

Fragile: must be configured per machine, breaks in subshells, invisible to tools that
invoke `claude` directly, and still broken at the API level per #4523.

Keeping CLAUDE.md short and specific — reduces compaction damage but doesn't prevent
it. Rules still degrade over long sessions. Mitigation, not a solution.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/34716
