---
layout: solution
title: "Config validation error causes infinite retry loop with no backoff — 1GB error log from single invalid key"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/29745
---

# Config validation error causes infinite retry loop with no backoff — 1GB error log from single invalid key

## 증상
When `openclaw.json` contains an unrecognized config key (e.g., `channels.whatsapp.groups.*.groupPolicy`), the gateway enters an infinite config validation retry loop with **zero backoff**. The same error message is written to `gateway.err.log` on every cycle, causing the log file to grow to **~1GB** within hours.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
1. OpenClaw 최신 버전으로 업데이트: `npm update -g openclaw`
2. Gateway 재시작: `openclaw gateway restart`
3. 설정 파일 확인: `~/.openclaw/config.yaml`
4. 로그 확인: `openclaw logs --tail 50`
5. 원본 GitHub Issue에서 패치 확인

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/29745
