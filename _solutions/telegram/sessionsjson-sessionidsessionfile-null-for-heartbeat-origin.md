---
layout: solution
title: "sessions.json sessionId/sessionFile null for heartbeat-origin sessions"
category: telegram
source: https://github.com/openclaw/openclaw/issues/51066
description: "Sessions initialized via do not write or back to . This causes to return empty despite real transcript content existing on"
---

# sessions.json sessionId/sessionFile null for heartbeat-origin sessions

## 증상
Sessions initialized via `origin.provider=heartbeat` do not write `sessionId` or `sessionFile` back to `sessions.json`. This causes `sessions_history` to return empty `[]` despite real transcript content existing on disk.

## 원인
Telegram Bot API conflict, rate limit, or webhook/polling configuration error causing message delivery failure.

## 해결법
Reading transcript `.jsonl` files directly from disk. Manual `sessions.json` patches are overwritten on gateway restart.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/51066
