---
layout: solution
title: "CLI → Gateway WebSocket connections timeout in v2026.3.12+ (Regression)"
category: openclaw
---

# CLI → Gateway WebSocket connections timeout in v2026.3.12+ (Regression)

## 증상
Regression (worked before, now fails)

에러 메시지:
```shell
Root cause: PR #44089 ("Gateway/WebSocket: add per-connection handshake timeout") changed DEFAULT_HANDSHAKE_TIMEOUT_MS from 10_000 (10s, v2026.3.11) to 3_000 (3s, v2026.3.12) in src/gateway/s

## 원인
원본 이슈에서 확인 필요. GitHub Issue #51438 참조.

## 해결법
.

### Additional information

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: openclaw
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/51438
