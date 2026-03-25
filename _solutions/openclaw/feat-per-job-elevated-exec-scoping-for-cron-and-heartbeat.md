---
layout: solution
title: "feat: per-job elevated exec scoping for cron and heartbeat"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/41484
---

# feat: per-job elevated exec scoping for cron and heartbeat

## 증상
`tools.elevated.allowFrom.heartbeat` and `tools.elevated.allowFrom.cron-event` only support wildcard (`*`) or no access. In a single-user gateway this is fine, but the moment you add additional users to `allowFrom` on any channel, those users can instruct the agent to create cron jobs or edit `HEARTBEAT.md` — and those jobs inherit elevated exec privileges via the wildcard.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Keep `cron-event: ["*"]` and do not add other users, or remove elevated from cron/heartbeat entirely and move privileged checks to OS-level cron.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/41484
