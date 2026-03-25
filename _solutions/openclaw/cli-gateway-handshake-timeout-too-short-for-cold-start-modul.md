---
layout: solution
title: "CLI gateway handshake timeout too short for cold-start module compilation"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/51469
---

# CLI gateway handshake timeout too short for cold-start module compilation

## 증상
CLI commands that connect to the gateway (e.g. `openclaw cron list`) fail with `gateway closed (1000 normal closure): no close reason` on systems where Node.js ESM module compilation takes longer than the gateway's 3-second WebSocket handshake timeout.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Patching the compiled dist files (`gateway-cli-*.js`) to change `DEFAULT_HANDSHAKE_TIMEOUT_MS` from `3e3` to `15e3`, then restarting the gateway. This fix is lost on every `openclaw` update.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/51469
