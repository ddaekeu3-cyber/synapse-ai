---
layout: solution
title: "Compaction trigger threshold (150K) does not scale with 1M context window"
category: context-window
source: https://github.com/anthropics/claude-code/issues/34202
---

# Compaction trigger threshold (150K) does not scale with 1M context window

## 증상
The auto-compaction trigger threshold is hardcoded at **150,000 tokens** server-side. This was reasonable for the default 200K context window (75% utilization), but with the new 1M context window (`context-1m-2025-08-07` beta), this same threshold triggers compaction at only **15% of the available context**.

## 원인
보고된 버그/문제. 카테고리: context-window.

## 해결법
The only available mechanism is the `PreCompact` hook with exit code 2 to block compaction, but this is a blunt instrument — it blocks all compaction rather than adjusting the threshold.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/34202
