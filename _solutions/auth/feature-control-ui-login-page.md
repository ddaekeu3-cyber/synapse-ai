---
layout: solution
title: "[Feature] Control UI Login Page 改进"
category: auth
source: https://github.com/openclaw/openclaw/issues/40127
---

# [Feature] Control UI Login Page 改进

## 증상
- [~] 支持 OAuth 登录流程优化 (UI 已完成，后端 API 待开发)

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
https://github.com/openclaw/openclaw/issues/40127
