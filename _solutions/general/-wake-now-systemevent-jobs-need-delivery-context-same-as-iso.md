---
layout: solution
title: "--wake now systemEvent jobs need delivery context (same as isolated)"
category: general
source: https://github.com/openclaw/openclaw/issues/34572
---

# --wake now systemEvent jobs need delivery context (same as isolated)

## 증상
When a cron job uses `--system-event` with `--wake now`, the agent wakes via immediate heartbeat but has **no inbound message**. Without a message source, the agent has no delivery context for its response.

## 원인
보고된 버그/문제. 카테고리: general.

## 해결법
Embed delivery instructions in the systemEvent text for the agent to parse:
```
[callback] Result | RESPOND_TO:discord:channel:123
```

This requires the agent to handle routing logic that should be infrastructure-level.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/34572
