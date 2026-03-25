---
layout: solution
title: "Gateway memory leak: 389MB → 14.7GB over 4 days with session accumulation"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/54155
---

# Gateway memory leak: 389MB → 14.7GB over 4 days with session accumulation

## 증상
The `openclaw-gateway` process leaks memory steadily over multi-day uptime, growing from ~389MB at startup to 14.7GB (58% of 24GB RAM on Mac Mini) after approximately 4 days of continuous operation.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
We implemented a daily maintenance LaunchAgent that:
- Kills the gateway at 4 AM (auto-restarts via LaunchAgent service)
- Resets any session JSONL files > 500KB
- Deletes old `.reset.*` files > 3 days
- Tightened compaction: `maxHistoryShare: 0.3`, pruning TTL `2h`, `keepLastAssistants: 2`

This keeps memory under control but doesn't fix the underlying leak.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/54155
