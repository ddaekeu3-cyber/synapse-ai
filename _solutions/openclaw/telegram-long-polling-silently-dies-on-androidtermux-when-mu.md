---
layout: solution
title: "Telegram long-polling silently dies on Android/Termux when multi-agent gateway is under load"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/32048
---

# Telegram long-polling silently dies on Android/Termux when multi-agent gateway is under load

## 증상
- **OpenClaw version:** 2026.2.26 (bc50708)

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
- `kill -9 <PID>` and manually restart the gateway
- Periodically clean session files to prevent accumulation
- Move large session `.jsonl` files to backup before restart

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/32048
