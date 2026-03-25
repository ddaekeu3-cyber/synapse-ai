---
layout: solution
title: "openclaw logs: WS handshake timeout due to event loop starvation during module loading"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/46885
---

# openclaw logs: WS handshake timeout due to event loop starvation during module loading

## 증상
`openclaw logs` fails with `gateway connect failed: Error: gateway closed (1000):` on every invocation. The gateway is healthy — `openclaw doctor`, control UI, and all channels work fine.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
```sh
# Read logs directly
tail -f ~/.openclaw/logs/gateway.log

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/46885
