---
layout: solution
title: "Bug: openclaw logs WebSocket handshake timeout in WSL2"
category: openclaw
---

# Bug: openclaw logs WebSocket handshake timeout in WSL2

## 증상
**Version:** OpenClaw 2026.3.13 (61d171a)

에러 메시지:
`
2. Observe "gateway connect failed: Error: gateway closed (1000)"

**Observed behavior:**
- HTTP health check works: `

## 원인
원본 이슈에서 확인 필요. GitHub Issue #47952 참조.

## 해결법
**
Use `tail -f /tmp/openclaw/openclaw-2026-03-16.log` instead

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: openclaw
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/47952
