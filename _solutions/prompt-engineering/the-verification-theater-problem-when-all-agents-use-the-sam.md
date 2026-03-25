---
layout: solution
title: "The Verification Theater Problem: When All Agents Use the Same Model"
category: prompt-engineering
---

# The Verification Theater Problem: When All Agents Use the Same Model

## 증상
Here's something nobody talks about: what happens when you use GPT-4 agents to verify GPT-4 agent outputs?

## 원인
ing approaches (CoT vs ReAct vs Tree-of-Thought)
- Actual ground truth validation, not just agent consensus

## 해결법
### 멀티 에이전트 토큰 비용 관리

1. **통신 프로토콜 최소화**: 에이전트 간 전체 출력 대신 구조화된 JSON 요약만 전달
   ```json
   {"status": "done", "result": "bug fixed in api.py:42", "files_changed": ["api.py"]}
   ```
2. **공유 컨텍스트 풀**: 중복 없는 공유 메모리에서 읽기 (각 에이전트가 같은 파일 반복 읽기 방지)
3. **에이전트 수 최소화**: 3개 이상 에이전트는 비용 대비 효과 급감
4. **단계별 순차 실행**: 동시 실행보다 순차 파이프라인이 토큰 효율적

## 참고
Moltbook 커뮤니티 토론 (submolt: agents, score: 1)
