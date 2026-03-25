---
layout: solution
title: "Docker build fails — TypeScript error in zod-schema.session.ts after #22520"
category: config
source: https://github.com/openclaw/openclaw/issues/22562
---

# Docker build fails — TypeScript error in zod-schema.session.ts after #22520

## 증상
Docker build (`pnpm build`) fails with `TS2769: No overload matches this call` in `src/config/zod-schema.session.ts:171` because the `.default()` object is missing the newly added required `ownerDisplay` property introduced in #22520.

## 원인
보고된 버그/문제. 카테고리: config.

## 해결법
1. 공식 문서 참조: 최신 설정 가이드 확인
2. 환경변수 확인: 필수 변수 설정 확인
3. 버전 호환성: 설정 포맷이 현재 버전과 맞는지 확인
4. 로그 확인: 시작 로그에서 설정 관련 경고 확인
5. 최소 설정으로 시작해서 하나씩 추가

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/22562
