---
layout: solution
title: "SQLITE_CANTOPEN crash when plugins.slots.memory is set to openclaw-mem0 (v2026.3.1)"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/32095
---

# SQLITE_CANTOPEN crash when plugins.slots.memory is set to openclaw-mem0 (v2026.3.1)

## 증상
Gateway enters a crash loop with `SQLITE_CANTOPEN` when `plugins.slots.memory` is set to `"openclaw-mem0"`. The gateway boots fine and channels connect, but it crashes during the `before_agent_start` hook when processing any inbound message.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Set `plugins.slots.memory` to `"none"` in `openclaw.json`. The mem0 plugin continues to work as a regular plugin (autoRecall/autoCapture active), just not occupying the builtin memory slot.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/32095
