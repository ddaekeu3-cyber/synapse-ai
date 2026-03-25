---
layout: solution
title: "bug(whatsapp): proactive send fails with No active WhatsApp Web listener when multiple accounts configured"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/50383
---

# bug(whatsapp): proactive send fails with No active WhatsApp Web listener when multiple accounts configured

## 증상
Proactive message sending via the `message` tool fails with `UNAVAILABLE` error when WhatsApp has multiple accounts configured (e.g. `bantito` + `default`). Reactive auto-reply works fine.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
None permanent. Hard gateway restart helps momentarily but fails again after reconnection cycles.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/50383
