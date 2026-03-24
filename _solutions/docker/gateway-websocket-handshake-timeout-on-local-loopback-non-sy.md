---
layout: solution
title: "Gateway WebSocket handshake timeout on local loopback (non-systemd env) breaks all CLI RPC commands"
category: docker
---

# Gateway WebSocket handshake timeout on local loopback (non-systemd env) breaks all CLI RPC commands

## 증상
Behavior bug (incorrect output/state without crash)

에러 메시지:
`
**4.** Wait ~30 seconds -> error:



### Expected behavior

**1.** `

## 원인
원본 이슈에서 확인 필요. GitHub Issue #52766 참조.

## 해결법
.

**Frequency:** Always - reproduced consistently across multiple attempts 
over multiple days (2026-03-17 and 2026-03-23). Every single CLI→gateway 
connection attempt hit the handshake timeout without exception.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: docker
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/52766
