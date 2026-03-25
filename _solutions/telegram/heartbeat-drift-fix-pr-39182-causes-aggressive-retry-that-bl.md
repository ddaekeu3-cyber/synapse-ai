---
layout: solution
title: "Heartbeat drift fix (PR #39182) causes aggressive retry that blocks Telegram during active conversations"
category: telegram
source: https://github.com/openclaw/openclaw/issues/40611
---

# Heartbeat drift fix (PR #39182) causes aggressive retry that blocks Telegram during active conversations

## 증상
After upgrading to 2026.3.7, heartbeats now consistently block Telegram message handling during active conversations. This was not observable before 3.7. The root cause appears to be PR #39182 (heartbeat scheduling drift fix), which changed the retry behavior after a `requests-in-flight` skip from advancing `nextDueMs` by the full interval to retrying via `DEFAULT_RETRY_MS` (~1 second).

## 원인
보고된 버그/문제. 카테고리: telegram.

## 해결법
correctly stops advancing the schedule on skip, but the wake layer's `DEFAULT_RETRY_MS` takes over, retrying every ~1 second. The heartbeat fires the **instant** the user's conversation turn completes and the session lock is released. The heartbeat then holds the session write lock for its entire embedded run (up to 600s hardcoded timeout), blocking all subsequent Telegram messages.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/40611
