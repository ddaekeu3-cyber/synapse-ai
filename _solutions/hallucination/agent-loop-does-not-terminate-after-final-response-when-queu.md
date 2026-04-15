---
layout: solution
title: "Agent loop does not terminate after final response when Queued messages exist in context — causes full task replay"
category: hallucination
source: https://github.com/openclaw/openclaw/issues/50956
description: "Behavior bug (incorrect output/state without"
---

# Agent loop does not terminate after final response when Queued messages exist in context — causes full task replay

## 증상
Behavior bug (incorrect output/state without crash)

## 원인
Model generated plausible but incorrect output due to insufficient grounding, missing verification, or high sampling temperature.

## 해결법
Delete session JSONL files and restart. (Restart alone is insufficient.)

  ## Related Issues

  - **#30604** — `Followup queue delivers same message multiple times when agent is busy`: upstream/related at the queue layer. PR #46170 was opened to fix it but
  closed by the author without merging.
  - **#35092** — `/new does not flush queued messages`: corroborates why session deletion is required for recovery.
  - **#50892** — Discord collect-mode duplicate delivery: superficially similar but different mechanism; confirmed **not the same issue**.

  The core problem described here — **agent lo

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/50956
