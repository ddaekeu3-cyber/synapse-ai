---
layout: solution
title: "Feature: configurable inbound message debounce for Telegram (channels.telegram.inboundDebounceMs)"
category: telegram
source: https://github.com/openclaw/openclaw/issues/48228
---

# Feature: configurable inbound message debounce for Telegram (channels.telegram.inboundDebounceMs)

## 증상
When composing a long message in Telegram using the **keyboard voice-to-text button** (speech transcription before sending), or when **pasting a large block of text** into the message field, Telegram splits the resulting text into multiple sequential messages on send.

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
https://github.com/openclaw/openclaw/issues/48228
