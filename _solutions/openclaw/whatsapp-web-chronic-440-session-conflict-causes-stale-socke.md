---
layout: solution
title: "WhatsApp Web: chronic 440 session conflict causes stale-socket loop and delivery failures"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/48390
---

# WhatsApp Web: chronic 440 session conflict causes stale-socket loop and delivery failures

## 증상
WhatsApp Web connection enters a self-reinforcing loop: the health monitor restarts the provider after 30 min of no events, but each new connection gets a **440 session conflict** from WhatsApp and immediately stops. This repeats indefinitely (51+ times in a single day), causing all outbound delivery to fail while appearing "connected" in the logs.

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
https://github.com/openclaw/openclaw/issues/48390
