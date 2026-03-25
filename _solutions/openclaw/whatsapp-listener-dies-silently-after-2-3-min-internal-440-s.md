---
layout: solution
title: "WhatsApp listener dies silently after ~2-3 min — internal 440 session conflict on single-process gateway"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/46188
---

# WhatsApp listener dies silently after ~2-3 min — internal 440 session conflict on single-process gateway

## 증상
WhatsApp Web listener connects successfully but dies silently after ~2-3 minutes. The gateway auto-restart creates a new connection before the old one tears down, causing an internal session conflict (status 440) — even with only one gateway process and no external WhatsApp Web sessions.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
None found. Fresh credentials, QR re-pairing, web heartbeat tuning, and `openclaw doctor` do not resolve the issue. The listener consistently dies within 2-3 minutes of connecting.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/46188
