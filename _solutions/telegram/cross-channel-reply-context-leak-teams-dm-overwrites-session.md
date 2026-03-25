---
layout: solution
title: "Cross-channel reply context leak — Teams DM overwrites session reply target, causing heartbeat responses to route to wrong user"
category: telegram
source: https://github.com/openclaw/openclaw/issues/24148
---

# Cross-channel reply context leak — Teams DM overwrites session reply target, causing heartbeat responses to route to wrong user

## 증상
When a Microsoft Teams DM arrives from a non-admin user (e.g., an employee), it overwrites the main session's reply context. If the Teams provider then crashes and recovers, and a heartbeat fires in the same session, the heartbeat response is delivered to the Teams DM user instead of the configured primary channel (Telegram). This caused private business data (financial figures, security alerts) t

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
https://github.com/openclaw/openclaw/issues/24148
