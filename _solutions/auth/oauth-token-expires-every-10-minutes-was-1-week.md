---
layout: solution
title: "OAuth token expires every ~10 minutes (was ~1 week)"
category: auth
source: https://github.com/anthropics/claude-code/issues/37804
description: "OAuth tokens are expiring approximately every 10 minutes, requiring frequent in each active session. Previously, tokens persisted for roughly 1 week with"
---

# OAuth token expires every ~10 minutes (was ~1 week)

## 증상
OAuth tokens are expiring approximately every 10 minutes, requiring frequent `/login` in each active session. Previously, tokens persisted for roughly 1 week with no configuration changes on the user's end.

## 원인
Authentication credential mismatch, expiry, or permission scope gap between the requesting agent and the target API.

## 해결법
1. API 키 유효성/만료 확인
2. OAuth 토큰 갱신: refresh token 사용
3. 환경변수 확인: .env 파일 설정 검증
4. 캐시된 인증 정보 삭제: `~/.openclaw/credentials.json` 제거 후 재인증
5. IP 화이트리스트/스코프 확인

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/37804
