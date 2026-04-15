---
layout: solution
title: "session-memory hook replays action intent on boot, causing restart loops"
category: loop-stuck
source: https://github.com/openclaw/openclaw/issues/29812
description: "The built-in hook persists conversation summaries to . When a session includes self-repair actions (e.g., \"I detected wrong routing metadata, let me"
---

# session-memory hook replays action intent on boot, causing restart loops

## 증상
The built-in `session-memory` hook persists conversation summaries to `workspace/memory/`. When a session includes self-repair actions (e.g., "I detected wrong routing metadata, let me restart the gateway"), the summary captures the *intent to act* alongside the observation. On the next boot, the agent reads this memory and re-executes the action, even though the original trigger condition may sti

## 원인
Agent entered a retry or decision loop without an exit condition, consuming tokens indefinitely without making progress. 카테고리: loop-stuck.

## 해결법
Rename the poisoned memory file with a `.resolved` suffix (e.g., `2026-02-25-session-routing.md` → `2026-02-25-session-routing.md.resolved`). The session-memory hook doesn't load non-`.md` files.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/29812
