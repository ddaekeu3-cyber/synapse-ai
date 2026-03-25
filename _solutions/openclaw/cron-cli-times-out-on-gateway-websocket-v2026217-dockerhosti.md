---
layout: solution
title: "cron CLI times out on gateway WebSocket (v2026.2.17, Docker/Hostinger VPS)"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/19874
---

# cron CLI times out on gateway WebSocket (v2026.2.17, Docker/Hostinger VPS)

## 증상
`openclaw cron list` (and other cron CLI subcommands) time out after 30s trying to connect to the gateway WebSocket, even though the gateway process is running and `openclaw gateway status` reports "RPC probe: ok".

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Cron jobs continue to execute on schedule. Only the management CLI/API is affected. No workaround found for listing or modifying cron jobs via CLI.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/19874
