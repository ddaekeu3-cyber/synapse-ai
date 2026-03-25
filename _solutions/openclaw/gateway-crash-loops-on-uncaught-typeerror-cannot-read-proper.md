---
layout: solution
title: "Gateway crash-loops on uncaught TypeError: Cannot read properties of null (reading 'setServername') in undici connection pool"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/26539
---

# Gateway crash-loops on uncaught TypeError: Cannot read properties of null (reading 'setServername') in undici connection pool

## 증상
The OpenClaw gateway crashes repeatedly with an uncaught `TypeError` originating from `undici`'s TLS connection pooling code. Since OpenClaw has no top-level uncaught exception handler that survives this error, the entire gateway process dies. systemd auto-restarts it, but any in-flight responses are lost.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
None available without modifying openclaw source. systemd auto-restart mitigates but doesn't prevent the crashes or lost responses.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/26539
