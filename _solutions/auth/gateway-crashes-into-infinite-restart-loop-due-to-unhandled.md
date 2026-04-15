---
layout: solution
title: "Gateway crashes into infinite restart loop due to Unhandled Promise Rejection on LLM API failure"
category: auth
source: https://github.com/openclaw/openclaw/issues/23441
description: "When the LLM API call fails (e.g., due to an unauthorized model or 404/403 error), an Unhandled Promise Rejection occurs. This crashes the entire OpenClaw"
---

# Gateway crashes into infinite restart loop due to Unhandled Promise Rejection on LLM API failure

## 증상
When the LLM API call fails (e.g., due to an unauthorized model or 404/403 error), an Unhandled Promise Rejection occurs. This crashes the entire OpenClaw Node.js gateway process instantly, often leaving zombie processes that cause infinite restart loops.

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
https://github.com/openclaw/openclaw/issues/23441
