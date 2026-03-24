---
layout: solution
title: "Cron: Isolated sessions not executing"
category: openclaw
---

# Cron: Isolated sessions not executing

## 증상
Behavior bug (incorrect output/state without crash)



## 원인
원본 이슈에서 확인 필요. GitHub Issue #52806 참조.

## 해결법
es (all failed):
Disabled/re-enabled job
Deleted and recreated job
Removed wakeMode parameter
Changed to sessionTarget: "main" (different error)
Gateway restart
Multiple manual triggers
Workaround:
Using manual execution - user requests "执行日报" which triggers direct tool calls instead of cron.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: openclaw
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/52806
