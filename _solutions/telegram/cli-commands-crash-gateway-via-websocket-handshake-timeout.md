---
layout: solution
title: "CLI commands crash gateway via WebSocket handshake timeout"
category: telegram
---

# CLI commands crash gateway via WebSocket handshake timeout

## 증상
Crash (process/app exits or hangs)



## 원인
원본 이슈에서 확인 필요. GitHub Issue #53168 참조.

## 해결법
Avoid all openclaw CLI commands while the gateway is running. Read cron config directly from ~/.openclaw/cron/jobs.json instead.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: telegram
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/53168
