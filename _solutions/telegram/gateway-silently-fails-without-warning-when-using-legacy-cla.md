---
layout: solution
title: "Gateway silently fails without warning when using legacy CLAWDBOT_* env variables"
category: telegram
---

# Gateway silently fails without warning when using legacy CLAWDBOT_* env variables

## 증상
Behavior bug (incorrect output/state without crash)

에러 메시지:
```text
  System:
    OS: macOS / Linux / Windows (WSL2)
  Binaries:
    Node: 24.x (or 22.16+)
    pnpm: 9.x
  OpenClaw:
    CLI Version: 2026.3.23 (or latest main build)
```

### Steps to reproduce


## 원인
원본 이슈에서 확인 필요. GitHub Issue #53482 참조.

## 해결법
Introduce a pre-flight check in the configuration loader. Before the Gateway fully boots, scan the `process.env` for any keys starting with `CLAWDBOT_`. If found, log a highly visible warning:
`WARN: Legacy CLAWDBOT_* environment variables are no longer supported. Please run 'openclaw onboard' to migrate your configuration or rename them according to the latest documentation.`

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: telegram
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/53482
