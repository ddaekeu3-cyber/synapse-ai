---
layout: solution
title: "Feature: Native skill restore after context compaction"
category: context-window
source: https://github.com/openclaw/openclaw/issues/28367
---

# Feature: Native skill restore after context compaction

## 증상
After context compaction (auto-cleanup when context window fills up), agents lose track of which skills (SKILL.md files) were actively loaded. The agent knows **what** it was working on (via `/tmp/<agent>-current-task.md`), but not **which skills** provided the operational context.

## 원인
보고된 버그/문제. 카테고리: context-window.

## 해결법
We've implemented a convention-based approach: agents write an `## Active Skills` section in their current-task file with absolute paths to SKILL.md files. After compaction, the agent reads this section and manually re-loads each skill.

```markdown

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/28367
