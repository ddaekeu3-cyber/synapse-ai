---
layout: solution
title: "Discord: Slow DiscordMessageListener (300s+) causes WebSocket 1006 drops and gateway crash"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/42178
---

# Discord: Slow DiscordMessageListener (300s+) causes WebSocket 1006 drops and gateway crash

## 증상
**OS:** macOS 26.3.0 (arm64, Mac mini M4)

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
We currently treat Discord as output-only (alerts, digests) and route all real conversations through webchat, which avoids long processing times on the Discord gateway.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/42178
