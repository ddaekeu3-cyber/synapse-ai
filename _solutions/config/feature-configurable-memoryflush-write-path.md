---
layout: solution
title: "Feature: configurable memoryFlush write path"
category: config
source: https://github.com/openclaw/openclaw/issues/48294
description: "When (automatic session-to-memory compaction) fires, it writes to a hardcoded"
---

# Feature: configurable memoryFlush write path

## 증상
When `memoryFlush` (automatic session-to-memory compaction) fires, it writes to a hardcoded path:

## 원인
Environment variable, configuration file, or initialization parameter missing, malformed, or incorrectly scoped.

## 해결법
A shell snippet in daily/weekly housekeeping crons sweeps orphaned dated files from `memory/` root into the proper monthly subdirectory:

```bash
for f in ~/clawd/memory/[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*.md; do
  [ -f "$f" ] || continue
  month=$(basename "$f" | cut -c1-7)
  mkdir -p ~/clawd/memory/daily/"$month"
  mv "$f" ~/clawd/memory/daily/"$month"/
done
```

This works but is an unnecessary workaround for what could be a simple config option.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/48294
