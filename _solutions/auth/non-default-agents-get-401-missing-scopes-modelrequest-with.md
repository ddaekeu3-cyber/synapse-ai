---
layout: solution
title: "Non-default agents get 401 Missing scopes: model.request with OpenAI Codex OAuth — same token works for default agent"
category: auth
source: https://github.com/openclaw/openclaw/issues/28293
description: "Non-default agents (, ) fail with when attempting to use OpenAI Codex models via OAuth. The default agent () works perfectly with the exact same OAuth"
---

# Non-default agents get 401 Missing scopes: model.request with OpenAI Codex OAuth — same token works for default agent

## 증상
Non-default agents (`engineer`, `strategist`) fail with `401 Missing scopes: model.request` when attempting to use OpenAI Codex models via OAuth. The **default agent (`main`) works perfectly** with the exact same OAuth token.

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
https://github.com/openclaw/openclaw/issues/28293
