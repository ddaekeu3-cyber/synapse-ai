---
layout: solution
title: "Gateway crash-loops on config reload when node host pairing fails"
category: openclaw
---

# Gateway crash-loops on config reload when node host pairing fails

## 증상
After installing the `lossless-claw` plugin, the config file was updated (`meta.lastTouchedAt`, `plugins.installs`). The gateway detected the config change and triggered a SIGTERM self-restart.

에러 메시지:
```
gateway connect failed: GatewayClientRequestError: pairing required
node host gateway closed (1008): pairing required
```

## 원인
원본 이슈에서 확인 필요. GitHub Issue #52014 참조.

## 해결법
`).

## Timeline
- `04:01:59` — Config change detected (lossless-claw install)
- `04:02:02` — SIGTERM received (config reload)
- `04:02:03` — Shutdown cleanly
- `04:04:00` — Restart attempt: `pairing required` error (repeated)
- `10:13:57` — Manual restart via `openclaw doctor --fix`

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: openclaw
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/52014
