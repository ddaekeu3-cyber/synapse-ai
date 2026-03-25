---
layout: solution
title: "WhatsApp health-monitor reconnect leaves receiver in broken state (silent message drop)"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/45198
---

# WhatsApp health-monitor reconnect leaves receiver in broken state (silent message drop)

## 증상
After WhatsApp health-monitor triggers an automatic restart due to disconnection, the status shows `connected` but **no inbound messages are received**. A full `openclaw gateway restart` is required to restore functionality.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Run `openclaw gateway restart` after noticing missing messages.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/45198
