---
layout: solution
title: "Bug: Gateway periodically overwrites agent auth-profiles.json with stale OAuth token, causing persistent auth failures"
category: auth
source: https://github.com/openclaw/openclaw/issues/48153
---

# Bug: Gateway periodically overwrites agent auth-profiles.json with stale OAuth token, causing persistent auth failures

## 증상
After a successful Anthropic Claude OAuth login, the gateway process periodically overwrites `~/.openclaw/agents/main/agent/auth-profiles.json` with a **stale refresh token**, causing `OAuth token refresh failed` errors on subsequent requests. The root-level `~/.openclaw/auth-profiles.json` and `~/.claude/.credentials.json` remain correct — only the agent-level copy drifts.

## 원인
보고된 버그/문제. 카테고리: auth.

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
https://github.com/openclaw/openclaw/issues/48153
