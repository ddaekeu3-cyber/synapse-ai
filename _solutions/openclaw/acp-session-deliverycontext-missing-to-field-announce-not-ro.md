---
layout: solution
title: "ACP session deliveryContext missing 'to' field — announce not routing back to requester channel (Telegram forum/topic)"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/33049
---

# ACP session deliveryContext missing 'to' field — announce not routing back to requester channel (Telegram forum/topic)

## 증상
When spawning an ACP session via `sessions_spawn` with `runtime="acp"` from a Telegram forum topic session, the ACP session's `deliveryContext` only contains `{ "channel": "telegram" }` — the `to` field (e.g. `telegram:-1003827078672`) is never inherited from the requester session. As a result, task completions never announce back to the originating chat.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
1. OpenClaw 최신 버전으로 업데이트: `npm update -g openclaw`
2. Gateway 재시작: `openclaw gateway restart`
3. 설정 파일 확인: `~/.openclaw/config.yaml`
4. 로그 확인: `openclaw logs --tail 50`
5. 원본 GitHub Issue에서 패치 확인

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/33049
