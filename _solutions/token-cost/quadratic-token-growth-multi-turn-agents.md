---
layout: solution
title: "Quadratic token cost growth in multi-turn agent loops"
category: token-cost
source: Stevens Institute - Hidden Economics of AI Agents
---

# Quadratic token cost growth in multi-turn agent loops

## 증상
Agent costs escalate rapidly during multi-turn conversations. A single task that should cost $0.50 ends up costing $5-8. Token usage grows quadratically because each turn resends the entire conversation history.

## 원인
LLMs charge for every input token in every turn. In multi-turn agent loops, the full conversation history is sent with each new turn, causing costs to grow quadratically rather than linearly. An unconstrained agent can cost $5-8 per software engineering task.

## 해결법
### Quadratic cost growth 해결법

1. **프롬프트 캐싱 활용**
   - 동일한 시스템 프롬프트/지시문은 캐싱하면 입력 비용 ~90% 절감
   - 대부분의 주요 API 제공자가 prompt caching 지원
   ```
   # Anthropic API example
   cache_control: {"type": "ephemeral"}
   ```

2. **동적 턴 제한 설정**
   - 성공 확률 기반으로 턴 수 제한 → 비용 24% 절감 + 성능 유지
   - 3회 실패 시 다른 접근법으로 전환

3. **컨텍스트 윈도우 관리**
   - 이전 턴의 전체 출력 대신 요약만 유지
   - 도구 호출 결과는 핵심 정보만 보존

4. **라우팅 패턴**
   - 작업 복잡도에 따라 모델 선택 자동화
   - 단순 작업: Haiku ($0.25/M), 복잡한 작업: Opus ($15/M)

## 예상 토큰 절약
이 에러로 삽질 시: 약 50,000~200,000 토큰 소비
이 해결법 참조 시: 약 5,000 토큰

## 출처
Stevens Institute - Hidden Economics of AI Agents
