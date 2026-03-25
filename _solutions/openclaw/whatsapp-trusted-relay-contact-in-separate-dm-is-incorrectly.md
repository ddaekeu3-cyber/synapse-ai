---
layout: solution
title: "WhatsApp trusted relay contact in separate DM is incorrectly told relay to main user is unavailable"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/53208
---

# WhatsApp trusted relay contact in separate DM is incorrectly told relay to main user is unavailable

## 증상
A trusted relay contact (Jill) is explicitly approved to send messages, links, media, and voice notes to Jack through OpenClaw on WhatsApp. Outbound sending to both contacts works. However, when Jill asks OpenClaw in her own direct WhatsApp chat to relay a message to Jack (for example, “tell Jack X”), OpenClaw incorrectly responds as if Jack’s chat/session is unavailable from that chat.

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
https://github.com/openclaw/openclaw/issues/53208
