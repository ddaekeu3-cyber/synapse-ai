---
layout: solution
title: "Priority lanes: interactive messages should bypass queued system events"
category: telegram
source: https://github.com/openclaw/openclaw/issues/53076
---

# Priority lanes: interactive messages should bypass queued system events

## 증상
When multiple system events (diff-triggered heartbeats, exec failure notifications, stall recovery dispatches) pile into the main agent session queue simultaneously, interactive user messages (e.g. Telegram DMs) get blocked behind them. Observed a 5-minute wait (`waitedMs=299279`) for a user message because a heartbeat + diff-trigger + exec events were queued ahead.

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
https://github.com/openclaw/openclaw/issues/53076
