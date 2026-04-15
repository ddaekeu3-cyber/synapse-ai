---
layout: solution
title: "Compact prompt should preserve historical context across multiple compactions"
category: memory
source: https://github.com/anthropics/claude-code/issues/33212
description: "When auto-compact fires, the default prompt focuses on recent work. After 2-3 compactions in the same session, all reasoning, decisions, and context from"
---

# Compact prompt should preserve historical context across multiple compactions

## 증상
When auto-compact fires, the default prompt focuses on **recent** work. After 2-3 compactions in the same session, **all reasoning, decisions, and context from earlier compactions is completely lost**. The model doesn''t even know there WAS a previous compaction.

## 원인
Agent session state was not persisted to durable storage, causing context to be lost on restart or session switch.

## 해결법
above) |
| 6-8 | Acceptable | High-level road preserved, implementation details gone |
| 9+ | Degraded | Even cumulative summaries get compressed, signal-to-noise drops |

**Recommendation for docs:** Switch sessions when the task is DONE. One session = one task/feature. Not one session forever, not a new session every 30 minutes. Without this guidance, users either restart too often (losing warm context - files read, patterns learned, user preferences) or never restart (context becomes soup after 10+ compactions).

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/33212
