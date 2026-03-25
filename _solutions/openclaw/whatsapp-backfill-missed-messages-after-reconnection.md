---
layout: solution
title: "WhatsApp: Backfill missed messages after reconnection"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/50093
---

# WhatsApp: Backfill missed messages after reconnection

## 증상
When the WhatsApp Web connection drops (e.g. 503 errors) and reconnects, messages sent to monitored groups during the outage window are never delivered or logged. The gateway reconnects successfully but the missed messages are silently lost.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Currently none — missed group messages are unrecoverable through the log workaround.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/50093
