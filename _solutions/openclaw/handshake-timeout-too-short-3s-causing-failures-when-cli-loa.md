---
layout: solution
title: "Handshake timeout too short (3s), causing failures when CLI loads plugins"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/47889
---

# Handshake timeout too short (3s), causing failures when CLI loads plugins

## 증상
In OpenClaw 3.12, the `DEFAULT_HANDSHAKE_TIMEOUT_MS` constant was changed from 10 seconds (10000ms) to 3 seconds (3000ms). This timeout is too short for CLI startup when loading plugins, causing frequent handshake failures.

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
https://github.com/openclaw/openclaw/issues/47889
