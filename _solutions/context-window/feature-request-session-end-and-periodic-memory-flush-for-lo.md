---
layout: solution
title: "Feature request: session-end and periodic memory flush for long-lived thread sessions"
category: context-window
source: https://github.com/openclaw/openclaw/issues/53305
description: "Thread sessions (Discord threads used as long-running, single-topic workspaces) never trigger the pre-compaction memory flush because they rarely compact."
---

# Feature request: session-end and periodic memory flush for long-lived thread sessions

## 증상
Thread sessions (Discord threads used as long-running, single-topic workspaces) never trigger the pre-compaction memory flush because they rarely compact. The flush is tied to the compaction cycle, which only fires when the context window fills. Short-to-moderate thread conversations never hit that threshold, so their work is silently lost when the session expires or the day rolls over.

## 원인
Input exceeded the model's maximum context length, causing truncation or a refusal to process the full request. 카테고리: context-window.

## 해결법
Agents can be instructed (via AGENTS.md) to proactively write to daily memory files during conversation rather than relying on the pre-compaction flush. This is fragile and depends on the agent remembering to do it.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/53305
