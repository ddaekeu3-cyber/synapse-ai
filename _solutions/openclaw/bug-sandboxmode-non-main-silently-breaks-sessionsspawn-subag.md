---
layout: solution
title: "Bug: sandbox.mode: 'non-main' silently breaks sessions_spawn subagent initialization"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/39248
---

# Bug: sandbox.mode: "non-main" silently breaks sessions_spawn subagent initialization

## 증상
When `agents.defaults.sandbox.mode` is set to `"non-main"`, sub-agents spawned via `sessions_spawn` fail to initialize. Sessions are accepted (return valid `childSessionKey` and `runId`) but never start executing — zero tokens, zero messages, no transcript files created. **No errors appear in gateway logs.**

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Set `agents.defaults.sandbox.mode: "off"` and use per-agent `sandbox.mode: "all"` overrides for agents that specifically need sandboxing (e.g., coding agents with `tools.exec.host: "sandbox"`).

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/39248
