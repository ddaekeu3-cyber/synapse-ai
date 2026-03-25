---
layout: solution
title: "Memory flush blocks user messages (isCompacting lock)"
category: general
source: https://github.com/openclaw/openclaw/issues/44032
---

# Memory flush blocks user messages (isCompacting lock)

## 증상
Memory flush runs as a pre-compaction step that executes an LLM turn. During this time, `isCompacting()` returns `true`, which blocks user message queuing. From the user's perspective, the bot goes silent for the duration of the flush — which can be significant if the session is large.

## 원인
보고된 버그/문제. 카테고리: general.

## 해결법
- Set `softThresholdTokens: 10000` and `forceFlushTranscriptBytes: "1MB"` to trigger flush earlier when sessions are smaller, reducing flush duration
- This reduces but does not eliminate the blocking window

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/44032
