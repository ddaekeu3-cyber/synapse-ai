---
layout: solution
title: "Add 'compact context' option when accepting plans in Plan Mode"
category: context-window
source: https://github.com/anthropics/claude-code/issues/38244
---

# Add 'compact context' option when accepting plans in Plan Mode

## 증상
When accepting a plan in Plan Mode, the only context management option available is "clear context" (enabled via `showClearContextOnPlanAccept`). This is a binary choice: either keep the full context (with all exploration cruft) or wipe it entirely.

## 원인
보고된 버그/문제. 카테고리: context-window.

## 해결법
Accept plan → manually run `/compact` → Claude resumes execution. Works, but adds friction to every plan cycle.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/38244
