---
layout: solution
title: "Token usage data drops between streaming and session persistence"
category: token-cost
source: https://github.com/openclaw/openclaw/issues/54218
---

# Token usage data drops between streaming and session persistence

## 증상
Token usage metrics are lost between the streaming response and session storage. Dashboard shows 0 tokens for sessions that actually consumed tokens. Cannot track real costs.

## 원인
pi-ai stream response does not correctly pass usage metadata to the session persistence layer. Field name mismatch between provider response and storage schema.

## 해결법
### 토큰 사용량 추적 누락 해결
1. Provider 응답의 usage 필드 매핑 확인
2. `usageMetadata` vs `usage` 필드명 불일치 패치
3. 세션 저장 시 usage 데이터가 null이면 로그 경고
4. 수동 추적: API 응답 헤더의 usage 정보 직접 파싱

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/54218
