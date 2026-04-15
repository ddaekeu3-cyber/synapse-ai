---
layout: solution
title: "Multi-agent teams multiply token costs exponentially"
category: token-cost
source: Medium - Markus Sandelin (AI Agent Teams Burning Money)
description: "Running multiple agents in a team (planner + executor + reviewer) causes token costs to grow much faster than expected. 3 agents ≠ 3x cost, often"
---

# Multi-agent teams multiply token costs exponentially

## 증상
Running multiple agents in a team (planner + executor + reviewer) causes token costs to grow much faster than expected. 3 agents ≠ 3x cost, often 10-20x.

## 원인
Each agent receives full context from other agents. Communication overhead between agents adds tokens. Planner output becomes executor input becomes reviewer input, each time growing.

## 해결법
### 멀티 에이전트 비용 폭발 방지

1. **통신 프로토콜 최소화**:
   - 에이전트 간 전체 출력 대신 구조화된 요약만 전달
   ```json
   {"action": "fix_bug", "file": "api.py", "line": 42, "fix": "add null check"}
   ```

2. **에이전트 수 최소화**: 정말 필요한 역할만 분리
3. **단계별 실행**: 동시 실행 대신 순차 파이프라인 (불필요한 중복 방지)
4. **공유 컨텍스트 풀**: 중복 없이 공유 메모리에서 읽기
5. **비용 한도 설정**: 팀 전체 토큰 예산 설정 + 초과 시 중단

## 예상 토큰 절약
이 에러로 삽질 시: 약 100,000~500,000 토큰 소비
이 해결법 참조 시: 약 10,000 토큰

## 출처
Medium - Markus Sandelin (AI Agent Teams Burning Money)
