---
layout: solution
title: "cron edit --light-context flag accepted but not persisted"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/31425
description: "accepts the flag without error but does not persist to the job"
---

# cron edit --light-context flag accepted but not persisted

## 증상
`openclaw cron edit <id> --light-context` accepts the flag without error but does not persist `lightContext` to the job payload.

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
Edit `~/.openclaw/cron/jobs.json` directly and restart the gateway, or recreate the job with `cron add`.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/31425
