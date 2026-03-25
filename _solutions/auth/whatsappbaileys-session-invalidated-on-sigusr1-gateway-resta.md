---
layout: solution
title: "WhatsApp/Baileys session invalidated on SIGUSR1 gateway restart — needs graceful WebSocket shutdown"
category: auth
source: https://github.com/openclaw/openclaw/issues/45730
---

# WhatsApp/Baileys session invalidated on SIGUSR1 gateway restart — needs graceful WebSocket shutdown

## 증상
When the gateway receives SIGUSR1 (config change, manual restart, update), the process exits immediately via supervisor restart. The WhatsApp Baileys WebSocket is killed abruptly without sending a proper disconnect to WhatsApp servers.

## 원인
보고된 버그/문제. 카테고리: auth.

## 해결법
Currently none. Avoiding gateway restarts minimizes occurrence but is not a real solution.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/45730
