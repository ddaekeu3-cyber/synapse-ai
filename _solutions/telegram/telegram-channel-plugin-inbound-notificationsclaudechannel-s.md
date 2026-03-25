---
layout: solution
title: "Telegram channel plugin: inbound notifications/claude/channel silently dropped"
category: telegram
source: https://github.com/anthropics/claude-code/issues/38534
---

# Telegram channel plugin: inbound notifications/claude/channel silently dropped

## 증상
The Telegram channel plugin (`claude-plugins-official/telegram` v0.0.4) successfully sends `notifications/claude/channel` via the MCP stdio transport, but Claude Code never surfaces inbound messages in the conversation. Outbound tools (reply, react, edit_message) work fine.

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
https://github.com/anthropics/claude-code/issues/38534
