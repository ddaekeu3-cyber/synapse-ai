---
layout: solution
title: "Add shell/exec payload type for cron jobs"
category: general
source: https://github.com/openclaw/openclaw/issues/50558
---

# Add shell/exec payload type for cron jobs

## 증상
Currently, cron jobs that need to run simple maintenance tasks (like clearing lock files) must use `agentTurn` payloads. This spawns a full agent session for each run, creating unnecessary overhead.

## 원인
보고된 버그/문제. 카테고리: general.

## 해결법
Use system crontab instead of Clawdbot cron for shell-only tasks.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/50558
