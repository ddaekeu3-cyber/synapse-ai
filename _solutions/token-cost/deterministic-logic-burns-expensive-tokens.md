---
layout: solution
title: "Agent uses expensive LLM tokens for tasks that should be deterministic"
category: token-cost
source: Medium - Alex Efimenko (7 Ways to Reduce AI Token Consumption)
---

# Agent uses expensive LLM tokens for tasks that should be deterministic

## 증상
Agent uses LLM API calls for simple deterministic tasks like email validation, date parsing, string formatting. Costs accumulate for tasks that could be done for free.

## 원인
No routing logic to separate deterministic tasks from tasks requiring LLM reasoning. Everything goes through the expensive model.

## 해결법
### 결정론적 작업에 토큰 낭비 방지

1. **태스크 분류기 구현**:
   ```python
   if is_deterministic(task):
       result = run_script(task)  # $0, 마이크로초
   else:
       result = llm.call(task)    # $$, 초 단위
   ```

2. **결정론적으로 할 수 있는 것들**:
   - 이메일 검증 → regex
   - 날짜 파싱 → dateutil
   - JSON 변환 → json.loads()
   - 파일 읽기/쓰기 → 직접 I/O
   - 수학 계산 → Python eval

3. **도구 우선 원칙**: LLM에게 "가능하면 도구를 먼저 사용하라" 지시
4. **비용 태깅**: 각 작업에 토큰 비용 로깅 → 비싼 결정론적 작업 식별

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
Medium - Alex Efimenko (7 Ways to Reduce AI Token Consumption)
