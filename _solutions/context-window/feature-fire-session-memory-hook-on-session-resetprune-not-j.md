---
layout: solution
title: "Feature: fire session-memory hook on session reset/prune, not just compaction"
category: context-window
source: https://github.com/openclaw/openclaw/issues/51572
---

# Feature: fire session-memory hook on session reset/prune, not just compaction

## 증상
The `session-memory` hook currently only fires during **auto-compaction** (when context window fills up). It does NOT fire when sessions are:

## 원인
보고된 버그/문제. 카테고리: context-window.

## 해결법
Building a custom cron script that scans `sessions.json` for sessions approaching `pruneAfter` and extracts summaries via API before the maintenance sweep runs.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/51572
