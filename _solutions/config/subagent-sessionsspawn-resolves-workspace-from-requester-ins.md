---
layout: solution
title: "Subagent sessions_spawn resolves workspace from requester instead of target agentId (regression in 2026.3.12)"
category: config
source: https://github.com/openclaw/openclaw/issues/45868
---

# Subagent sessions_spawn resolves workspace from requester instead of target agentId (regression in 2026.3.12)

## 증상
Regression (worked before, now fails)

## 원인
GitHub Issue #45868에서 보고된 버그. 해결법이 이슈에 포함됨.

## 해결법
When agentId is explicitly provided in sessions_spawn, resolve the workspace
  from the target agent's config (via resolveAgentWorkspaceDir(config, agentId))
  instead of the requester's session key. The requester-based inheritance should only
  apply when no explicit agentId is provided (anonymous subagent spawning).

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/45868
