---
layout: solution
title: "CLI gateway handshake timeout on WSL2 — 2026.3.13 regression"
category: docker
---

# CLI gateway handshake timeout on WSL2 — 2026.3.13 regression

## 증상
Regression (worked before, now fails)

에러 메시지:
```shell
**CLI output:**

$ openclaw gateway health --url ws://127.0.0.1:18789 --token "<redacted>"
gateway connect failed: Error: gateway closed (1000):
Error: gateway closed (1000 normal closure): n

## 원인
원본 이슈에서 확인 필요. GitHub Issue #51879 참조.

## 해결법
Setting `VITEST=1` and `OPENCLAW_TEST_HANDSHAKE_TIMEOUT_MS=10000` on both the gateway systemd service and CLI invocation allows the handshake to complete (~1166ms).

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: docker
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/51879
