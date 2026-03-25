---
layout: solution
title: "Compaction timeout: add escape hatch to prevent session hang"
category: context-window
source: https://github.com/openclaw/openclaw/issues/44031
---

# Compaction timeout: add escape hatch to prevent session hang

## 증상
When compaction times out, `selectCompactionTimeoutSnapshot()` falls back to an already-overflowed snapshot (e.g., 234k tokens in a 200k context window). The LLM call then hangs or fails repeatedly, blocking the entire lane. All subsequent messages to that agent queue up indefinitely — the bot appears "dead."

## 원인
보고된 버그/문제. 카테고리: context-window.

## 해결법
- Aggressive compaction settings (`maxHistoryShare: 0.4`, `recentTurnsPreserve: 3`, early memory flush)
- External `session_overflow_guard.sh` that scans `sessions.json` for >90% token usage and archives/removes overflow sessions
- Called from `self_heal.sh` (every 5 minutes via cron)

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/44031
