---
layout: solution
title: "Feature request: memory flush before /new and /restart session reset"
category: config
source: https://github.com/openclaw/openclaw/issues/41216
---

# Feature request: memory flush before /new and /restart session reset

## 증상
`/new` and `/restart` immediately destroy the session without any opportunity to persist context. The pre-compaction memory flush (`compaction.memoryFlush`) works great for auto-compaction, but session resets bypass it entirely.

## 원인
보고된 버그/문제. 카테고리: config.

## 해결법
Manually asking the agent to write session notes before running `/new`. Works but relies on human memory, which is ironic for a memory management feature.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/41216
