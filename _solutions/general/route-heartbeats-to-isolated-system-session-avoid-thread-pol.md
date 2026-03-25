---
layout: solution
title: "Route heartbeats to isolated system session (avoid thread pollution)"
category: general
source: https://github.com/openclaw/openclaw/issues/11393
---

# Route heartbeats to isolated system session (avoid thread pollution)

## 증상
When a heartbeat poll fires during an active conversation, the `HEARTBEAT_OK` response gets injected into the user's conversation thread, causing confusion.

## 원인
보고된 버그/문제. 카테고리: general.

## 해결법
Agents can detect mid-conversation state and skip heartbeat responses, but this is fragile and relies on agent-side logic.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/11393
