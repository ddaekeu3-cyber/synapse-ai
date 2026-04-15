---
layout: solution
title: "Agent stuck repeating the same failed action (looping)"
category: loop-stuck
source: Stevens Institute - Hidden Economics of AI Agents
description: "Agent attempts the same failing approach repeatedly without trying alternatives. Gets stuck in a retry loop, burning tokens on identical failed attempts."
---

# Agent stuck repeating the same failed action (looping)

## 증상
Agent attempts the same failing approach repeatedly without trying alternatives. Gets stuck in a retry loop, burning tokens on identical failed attempts. No exit condition triggers.

## 원인
Lack of dynamic exit criteria or state tracking. The agent has no mechanism to detect that it has already tried an approach and failed, so it keeps attempting the same action.

## 해결법
### 에이전트 루프 탈출법

1. **실패 히스토리 추적**
   ```python
   failed_approaches = []
   for attempt in range(max_attempts):
       approach = select_approach(exclude=failed_approaches)
       result = try_approach(approach)
       if result.failed:
           failed_approaches.append(approach)
       else:
           break
   ```

2. **동적 턴 제한**
   - 성공 확률 기반 동적 제한: 성공률 < 20%이면 중단
   - 연구 결과: 이 방식으로 비용 24% 절감 + 성능 유지

3. **에러 패턴 감지**
   - 같은 에러 메시지 3회 연속 → 자동 전환
   - 에러 유형별 대체 전략 매핑

4. **에스컬레이션**
   - 자동 해결 실패 시 사람에게 즉시 보고
   - "모르면 OK 처리하지 않는다" 원칙

## 예상 토큰 절약
이 에러로 삽질 시: 약 20,000~50,000 토큰 소비
이 해결법 참조 시: 약 2,000 토큰

## 출처
Stevens Institute - Hidden Economics of AI Agents
