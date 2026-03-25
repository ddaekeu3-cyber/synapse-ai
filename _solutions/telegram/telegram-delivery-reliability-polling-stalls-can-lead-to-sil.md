---
layout: solution
title: "Telegram delivery reliability: polling stalls can lead to silent outbound message loss"
category: telegram
source: https://github.com/openclaw/openclaw/issues/50040
---

# Telegram delivery reliability: polling stalls can lead to silent outbound message loss

## 증상
On OpenClaw 2026.3.12, Telegram Bot API connectivity may remain generally healthy while the gateway's Telegram polling loop intermittently stalls/restarts. During those recovery windows, outbound `sendMessage` delivery can fail and the effective recovery path is not strong enough at runtime, leading to silent or operator-visible message loss.

## 원인
보고된 버그/문제. 카테고리: telegram.

## 해결법
1. Bot Token 확인: BotFather에서 토큰 재발급
2. Webhook URL 설정 확인
3. 메시지 포맷 호환성 확인
4. Rate limit: Telegram API 제한 준수
5. 그룹 권한 설정 확인

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/50040
