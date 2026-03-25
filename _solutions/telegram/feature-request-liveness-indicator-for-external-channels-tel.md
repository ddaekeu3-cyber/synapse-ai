---
layout: solution
title: "Feature Request: Liveness indicator for external channels (Telegram, etc.) via WebSockets"
category: telegram
source: https://github.com/anthropics/claude-code/issues/36986
---

# Feature Request: Liveness indicator for external channels (Telegram, etc.) via WebSockets

## 증상
When using Claude Code with external channels (e.g., Telegram via the channel plugin), there is currently no way to know whether the Claude Code session is actively running and processing a request. This makes the experience feel opaque — messages are sent and you wait with no feedback.

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
https://github.com/anthropics/claude-code/issues/36986
