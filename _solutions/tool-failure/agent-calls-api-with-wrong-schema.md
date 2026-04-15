---
layout: solution
title: "Agent calls APIs with incorrect schemas or parameters"
category: tool-failure
source: Stevens Institute - Hidden Economics of AI Agents
description: "Agent invokes tools/APIs with wrong parameter types, missing required fields, or incorrect schemas. API calls return validation errors (400, 422)"
---

# Agent calls APIs with incorrect schemas or parameters

## 증상
Agent invokes tools/APIs with wrong parameter types, missing required fields, or incorrect schemas. API calls return validation errors (400, 422) repeatedly.

## 원인
Insufficient grounding in tool specifications. Agent relies on training data rather than actual API documentation, leading to outdated or incorrect parameter usage.

## 해결법
### API/도구 호출 실패 해결

1. **도구 스펙 명시적 주입**
   - 시스템 프롬프트에 정확한 API 스키마 포함
   - JSON Schema 형태로 파라미터 타입/필수값 명시

2. **요청 전 검증**
   ```python
   # 호출 전 스키마 검증
   from jsonschema import validate
   validate(instance=params, schema=tool_schema)
   ```

3. **에러 피드백 루프**
   - 422 에러 응답의 validation 메시지를 에이전트에게 피드백
   - "이 필드가 필수입니다" → 재시도 시 포함

4. **멀티 스텝 계획**
   - 도구 호출 전 계획 단계 추가
   - 계획 → 파라미터 확인 → 실행 → 검증

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
Stevens Institute - Hidden Economics of AI Agents
