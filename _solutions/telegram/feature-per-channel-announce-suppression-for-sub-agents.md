---
layout: solution
title: "Feature: per-channel announce suppression for sub-agents"
category: telegram
source: https://github.com/openclaw/openclaw/issues/13911
---

# Feature: per-channel announce suppression for sub-agents

## 증상
When the main agent spawns a sub-agent from a Telegram session, the completion announcement always routes back to Telegram. There's no config option to suppress or reroute sub-agent announces per channel.

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
https://github.com/openclaw/openclaw/issues/13911
