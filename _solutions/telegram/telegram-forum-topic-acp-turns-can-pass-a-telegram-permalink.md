---
layout: solution
title: "Telegram forum-topic ACP turns can pass a Telegram permalink to ACP instead of the actual message body"
category: telegram
source: https://github.com/openclaw/openclaw/issues/43899
---

# Telegram forum-topic ACP turns can pass a Telegram permalink to ACP instead of the actual message body

## 증상
Telegram forum-topic messages sent to a topic-bound ACP session can be serialized into the ACP prompt as a **Telegram permalink** (for example `https://t.me/c/<chat>/<topic>/<message>`) instead of the actual inbound message body.

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
https://github.com/openclaw/openclaw/issues/43899
