---
layout: solution
title: "minimax-portal: MiniMax-M2.7/M2.7-highspeed image vision fails with Unknown model"
category: auth
source: https://github.com/openclaw/openclaw/issues/53575
description: "- Provider: （MiniMax"
---

# minimax-portal: MiniMax-M2.7/M2.7-highspeed image vision fails with Unknown model

## 증상
- **Provider**: `minimax-portal`（MiniMax OAuth）

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
https://github.com/openclaw/openclaw/issues/53575
