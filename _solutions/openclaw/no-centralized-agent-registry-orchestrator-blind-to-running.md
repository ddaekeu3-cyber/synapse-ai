---
layout: solution
title: "No centralized agent registry - orchestrator blind to running agents at scale"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/35802
---

# No centralized agent registry - orchestrator blind to running agents at scale

## 증상
**Supersedes:** #7048 (locked/closed by stale-bot — not resolved)

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Manual AGENTS.md markdown table + heartbeat state JSON. Requires human update after every agent change.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/35802
