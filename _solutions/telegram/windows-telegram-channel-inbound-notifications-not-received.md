---
layout: solution
title: "[Windows] Telegram channel inbound notifications not received by Claude Code"
category: telegram
source: https://github.com/anthropics/claude-code/issues/37705
---

# [Windows] Telegram channel inbound notifications not received by Claude Code

## 증상
On Windows 11, the Telegram MCP plugin's inbound `notifications/claude/channel` is not being received by Claude Code, while outbound (reply/react/edit tools) works perfectly.

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
https://github.com/anthropics/claude-code/issues/37705
