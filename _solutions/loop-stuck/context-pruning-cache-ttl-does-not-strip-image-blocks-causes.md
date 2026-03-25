---
layout: solution
title: "Context pruning (cache-ttl) does not strip image blocks — causes compaction futility loop at scale"
category: loop-stuck
source: https://github.com/openclaw/openclaw/issues/39573
---

# Context pruning (cache-ttl) does not strip image blocks — causes compaction futility loop at scale

## 증상
`contextPruning.mode: "cache-ttl"` prunes old tool results and soft-trims large text blocks, but it does **not** prune `{"type": "image"}` content blocks from session history. At scale (6,600+ users), this causes a **compaction futility loop** where power users' sessions permanently bounce at the context ceiling, eventually getting stuck.

## 원인
보고된 버그/문제. 카테고리: loop-stuck.

## 해결법
We manually strip images from session JSONL files using an offline script. This is not sustainable at 6,600+ users and growing.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/39573
