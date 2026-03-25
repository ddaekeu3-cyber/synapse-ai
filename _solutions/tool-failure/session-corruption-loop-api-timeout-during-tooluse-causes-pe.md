---
layout: solution
title: "Session corruption loop: API timeout during tool_use causes permanent broken state"
category: tool-failure
source: https://github.com/openclaw/openclaw/issues/44196
---

# Session corruption loop: API timeout during tool_use causes permanent broken state

## 증상
When an embedded agent run times out mid-execution while a `tool_use` block is in-flight, the session JSONL file is saved in a corrupt state. Every subsequent message then triggers the same API error, creating an unrecoverable loop.

## 원인
보고된 버그/문제. 카테고리: tool-failure.

## 해결법
We built an external repair script that scans and cleans session files. Running it on our installation fixed 8 affected sessions (97 corrupt entries total — including sessions that had been accumulating corrupt entries silently for weeks). Happy to share the script if useful as a reference for the fix.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/44196
