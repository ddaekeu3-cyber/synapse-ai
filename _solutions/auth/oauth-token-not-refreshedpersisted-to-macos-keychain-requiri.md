---
layout: solution
title: "OAuth token not refreshed/persisted to macOS Keychain, requiring re-login every session"
category: auth
source: https://github.com/anthropics/claude-code/issues/38521
---

# OAuth token not refreshed/persisted to macOS Keychain, requiring re-login every session

## 증상
Claude Code requires re-authentication every time it's opened on macOS. The OAuth access token has a ~15 hour lifetime and the refresh token is not being used to persist a new access token back to Keychain.

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
https://github.com/anthropics/claude-code/issues/38521
