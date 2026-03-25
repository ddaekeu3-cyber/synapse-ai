---
layout: solution
title: "WhatsApp watchdog MESSAGE_TIMEOUT_MS (30min) not configurable — causes reconnect loops on low-traffic setups"
category: config
source: https://github.com/openclaw/openclaw/issues/53698
---

# WhatsApp watchdog MESSAGE_TIMEOUT_MS (30min) not configurable — causes reconnect loops on low-traffic setups

## 증상
The WhatsApp web monitor watchdog has a hardcoded `MESSAGE_TIMEOUT_MS` of 30 minutes (1800s). On low-traffic setups (e.g., DM-only with `dmPolicy: allowlist`), it's common to go hours without inbound messages. The watchdog treats this as a dead connection and force-closes it with status 499 ("watchdog-timeout"), triggering reconnect loops that eventually escalate to 440 (session conflict).

## 원인
보고된 버그/문제. 카테고리: config.

## 해결법
Gateway restart resets the `lastInboundAt` timer, temporarily clearing the loop. But it recurs after the next 30-minute idle window.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/53698
