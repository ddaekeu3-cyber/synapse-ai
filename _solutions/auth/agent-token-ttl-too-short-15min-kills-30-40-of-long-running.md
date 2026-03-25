---
layout: solution
title: "Agent token TTL too short (~15min) — kills 30-40% of long-running worktree agents"
category: auth
source: https://github.com/anthropics/claude-code/issues/37636
---

# Agent token TTL too short (~15min) — kills 30-40% of long-running worktree agents

## 증상
When using Claude Code with background worktree agents for complex multi-file tasks (code review, decomposition, security fixes), agents consistently hit login token expiry after ~15 minutes. The agent returns `Not logged in · Please run /login` with zero useful work committed.

## 원인
보고된 버그/문제. 카테고리: auth.

## 해결법
(re-dispatching) doubles token cost and context waste for every affected agent.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/37636
