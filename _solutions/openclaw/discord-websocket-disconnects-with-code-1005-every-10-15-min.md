---
layout: solution
title: "Discord WebSocket disconnects with code 1005 every 10-15 minutes, triggering health-monitor restarts"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/44227
---

# Discord WebSocket disconnects with code 1005 every 10-15 minutes, triggering health-monitor restarts

## 증상
- **Platform:** Raspberry Pi 5, Debian/arm64

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
- Increased `gateway.channelHealthCheckMinutes` to 10

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/44227
