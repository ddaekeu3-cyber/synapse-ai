---
layout: solution
title: "Gateway self-SIGTERMs after ~45s: server.mjs device approval timeout kills healthy gateway"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/51463
---

# Gateway self-SIGTERMs after ~45s: server.mjs device approval timeout kills healthy gateway

## 증상
On container restart, `server.mjs` spawns the gateway process but kills it with SIGTERM after ~45 seconds if "device approval" does not complete in time. This causes a crash loop when combined with a watchdog auto-restart.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
None from inside the container. The server.mjs timeout logic is baked into the Docker image.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/51463
