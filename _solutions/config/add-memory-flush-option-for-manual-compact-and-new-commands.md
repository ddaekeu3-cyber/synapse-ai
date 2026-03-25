---
layout: solution
title: "Add memory flush option for manual /compact and /new commands"
category: config
source: https://github.com/openclaw/openclaw/issues/23952
---

# Add memory flush option for manual /compact and /new commands

## 증상
The pre-compaction memory flush (configured via `agents.defaults.compaction.memoryFlush`) only triggers for **auto-compaction** when context crosses the soft threshold. Manual commands `/compact` and `/new` bypass this entirely.

## 원인
보고된 버그/문제. 카테고리: config.

## 해결법
User has to manually ask the agent to "save to memory" before running `/compact` or `/new`. Works but adds friction.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/23952
