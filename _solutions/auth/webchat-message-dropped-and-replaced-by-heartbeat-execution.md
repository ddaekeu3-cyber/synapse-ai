---
layout: solution
title: "Webchat message dropped and replaced by heartbeat execution on LLM Provider Error"
category: auth
source: https://github.com/openclaw/openclaw/issues/31552
---

# Webchat message dropped and replaced by heartbeat execution on LLM Provider Error

## 증상
When sending a message to the agent via the Webchat UI, if the underlying LLM provider (Anthropic, OpenAI, DeepSeek, etc.) returns a fatal error (like an `HTTP 401 authentication_error`), the user's message completely disappears from the UI. Immediately after, the UI gets hijacked by background "heartbeat" tool executions (e.g., `gog` email/chat checks).

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
https://github.com/openclaw/openclaw/issues/31552
