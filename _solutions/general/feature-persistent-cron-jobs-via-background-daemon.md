---
layout: solution
title: "Feature: Persistent cron jobs via background daemon"
category: general
source: https://github.com/anthropics/claude-code/issues/32806
---

# Feature: Persistent cron jobs via background daemon

## 증상
**CronCreate is great** — scheduling recurring prompts within a session is a powerful workflow. But jobs die when the session exits, which limits the feature to long-running sessions only.

## 원인
보고된 버그/문제. 카테고리: general.

## 해결법
es autonomously
- Runs the full test suite to verify

**The problem**: This only works while my session is open. I want this running every night regardless of whether I have Claude Code open. A daemon would make this "set and forget" — like a CI job but for continuous codebase health.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/32806
