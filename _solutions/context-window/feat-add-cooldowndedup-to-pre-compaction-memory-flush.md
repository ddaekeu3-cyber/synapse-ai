---
layout: solution
title: "feat: add cooldown/dedup to pre-compaction memory flush"
category: context-window
source: https://github.com/openclaw/openclaw/issues/52359
---

# feat: add cooldown/dedup to pre-compaction memory flush

## 증상
Pre-compaction memory flushes can fire back-to-back when the context window oscillates near the compaction threshold. In a fast-moving session with verbose tool output (TTS, voice memos, etc.), it's common to see two flushes within 60 seconds of each other — the second one always produces "NO_REPLY" (nothing new to store) and wastes an entire agentic turn of tokens.

## 원인
보고된 버그/문제. 카테고리: context-window.

## 해결법
Setting `softThresholdTokens: 2000` reduces frequency significantly since flushes only fire when within 2000 tokens of compaction. But a proper cooldown would prevent the edge case where rapid context growth triggers multiple flushes in quick succession.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/52359
