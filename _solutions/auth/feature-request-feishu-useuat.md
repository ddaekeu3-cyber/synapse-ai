---
layout: solution
title: "[Feature Request] Feishu: 支持 useUAT 参数以用户身份创建文档"
category: auth
source: https://github.com/openclaw/openclaw/issues/31501
---

# [Feature Request] Feishu: 支持 useUAT 参数以用户身份创建文档

## 증상
当前 feishu_doc 工具创建文档时只支持机器人身份（tenant_access_token），无法以用户身份创建。

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
https://github.com/openclaw/openclaw/issues/31501
