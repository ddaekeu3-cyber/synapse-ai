---
layout: solution
title: "Google Chat channel: 'Cannot convert undefined or null to object' on typing indicator and message send"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/40962
---

# Google Chat channel: 'Cannot convert undefined or null to object' on typing indicator and message send

## 증상
Google Chat channel fails to send messages (and typing indicators) with the error:

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
https://github.com/openclaw/openclaw/issues/40962
