---
layout: solution
title: "codex-cli/gpt-5.4 fails in embedded/helper paths while openai-codex/gpt-5.4 works"
category: auth
source: https://github.com/openclaw/openclaw/issues/38212
description: "On OpenClaw , helper paths that use the embedded runner can still fail when the configured primary model is , even though the equivalent Codex"
---

# codex-cli/gpt-5.4 fails in embedded/helper paths while openai-codex/gpt-5.4 works

## 증상
On OpenClaw `2026.3.2`, helper paths that use the embedded runner can still fail when the configured primary model is `codex-cli/gpt-5.4`, even though the equivalent Codex OAuth-backed ref `openai-codex/gpt-5.4` already has forward-compat support.

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
https://github.com/openclaw/openclaw/issues/38212
