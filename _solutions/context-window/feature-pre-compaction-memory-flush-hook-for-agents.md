---
layout: solution
title: "Feature: pre-compaction memory flush hook for agents"
category: context-window
source: https://github.com/openclaw/openclaw/issues/52314
description: "Expose a pre-compaction callback so agents can flush durable notes to disk before context is"
---

# Feature: pre-compaction memory flush hook for agents

## 증상
Expose a pre-compaction callback so agents can flush durable notes to disk before context is summarized/compacted.

## 원인
Input exceeded the model's maximum context length, causing truncation or a refusal to process the full request. 카테고리: context-window.

## 해결법
Currently mitigated via: strict MEMORY.md size limit (10k chars), weekly maintenance cron, and daily session notes to `memory/YYYY-MM-DD.md`. But this is process discipline, not a platform guarantee.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/52314
