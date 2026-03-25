---
layout: solution
title: "BlueBubbles (webhook channel) incorrectly triggers stale-socket health monitor restart during quiet periods"
category: general
source: https://github.com/openclaw/openclaw/issues/32990
---

# BlueBubbles (webhook channel) incorrectly triggers stale-socket health monitor restart during quiet periods

## 증상
The channel health monitor's stale-socket detection restarts the BlueBubbles provider after 30 minutes of no inbound events. For webhook-based channels like BlueBubbles, this is incorrect behavior — a quiet period simply means no messages were sent, not that the channel is broken.

## 원인
보고된 버그/문제. 카테고리: general.

## 해결법
Auto-heal cron every 10 min that checks for recent health-monitor restarts and resets the BB webhook. Works but shouldn't be necessary.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/32990
