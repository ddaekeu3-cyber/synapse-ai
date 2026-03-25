---
layout: solution
title: "Expose channels.whatsapp.staleEventThresholdMs config key for health monitor"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/51614
---

# Expose channels.whatsapp.staleEventThresholdMs config key for health monitor

## 증상
The WhatsApp channel health monitor declares a socket "stale" and kills the connection if no **inbound** message is received within 30 minutes. This threshold is hardcoded and cannot be configured, causing a death-loop on accounts with low inbound traffic.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Restart the gateway and send a message to the WhatsApp account within 30 minutes to prime `lastEventAt`. This is fragile and breaks again on the next quiet stretch.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/51614
