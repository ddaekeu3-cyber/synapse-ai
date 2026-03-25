---
layout: solution
title: "Cron/proactive message tool fails with 'No active WhatsApp Web listener' while auto-reply works — WSL2/Docker"
category: docker
source: https://github.com/openclaw/openclaw/issues/48747
---

# Cron/proactive message tool fails with "No active WhatsApp Web listener" while auto-reply works — WSL2/Docker

## 증상
Crash (process/app exits or hangs)

## 원인
GitHub Issue #48747에서 보고된 버그. 해결법이 이슈에 포함됨.

## 해결법
the listener
- Only inbound-triggered auto-replies work; all proactive messaging is broken
- Stuck delivery entries persist across restarts, growing with every failed attempt
Related issues: #14406, #30177

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/48747
