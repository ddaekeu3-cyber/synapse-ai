---
layout: solution
title: "[Feature] Graceful context compaction — PreCompact hook data + background compaction option"
category: tool-failure
source: https://github.com/anthropics/claude-code/issues/33088
description: "Context compaction is currently a hard wall — a synchronous operation that pauses the session, lossy-compresses the conversation history, and resumes with"
---

# [Feature] Graceful context compaction — PreCompact hook data + background compaction option

## 증상
Context compaction is currently a hard wall — a synchronous operation that pauses the session, lossy-compresses the conversation history, and resumes with degraded context. For long sessions (200+ tool calls), this happens multiple times and each compaction loses nuance, reasoning chains, and momentum.

## 원인
Tool or plugin call failed due to schema mismatch, missing parameter, permission error, or upstream API change. 카테고리: tool-failure.

## 해결법
We monitor context pressure via token counting from the active .jsonl transcript, fire relay baton checkpoints at 60% and 70% thresholds, and use a PreCompact hook to create emergency snapshots. This works but is fragile — the token count heuristic doesn't always match Claude Code's internal measurement, so we sometimes miss the window.

---

Built from daily production usage on Claude Code Max. The context wall is the single biggest friction point in long autonomous sessions.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/33088
