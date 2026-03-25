---
layout: solution
title: "WhatsApp outbound sends fail despite active inbound listener (2026.3.13)"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/50731
---

# WhatsApp outbound sends fail despite active inbound listener (2026.3.13)

## 증상
`openclaw message send --channel whatsapp` fails with `No active WhatsApp Web listener` even though the gateway log shows the WhatsApp provider started successfully and the inbound listener is active.

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
https://github.com/openclaw/openclaw/issues/50731
