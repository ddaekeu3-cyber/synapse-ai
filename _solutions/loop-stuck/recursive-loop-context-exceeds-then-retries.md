---
layout: solution
title: "Agent enters recursive loop: exceeds context → error → retry → same error"
category: loop-stuck
source: Arize AI - Managing Memory in AI Agents
---

# Agent enters recursive loop: exceeds context → error → retry → same error

## 증상
Agent constructs context that exceeds token limits, causing an error. The problematic span remains in session. Agent runs again on same data, hits same limit, loops forever.

## 원인
Failed context that exceeded limits remains in session state. No cleanup mechanism removes the problematic data. Each retry reconstructs the same oversized context.

## 해결법
### 컨텍스트 초과 재귀 루프 해결

1. **실패 시 컨텍스트 정리**:
   ```python
   try:
       response = llm.call(context)
   except ContextTooLongError:
       context = truncate_oldest(context, target=0.8 * max_tokens)
       # 실패한 데이터 제거 후 재시도
   ```

2. **사전 토큰 카운팅**: 호출 전 토큰 수 검증
   ```python
   if count_tokens(context) > max_tokens * 0.9:
       context = compress(context)
   ```

3. **최대 재시도 제한**: 같은 에러 3회 → 중단 + 보고
4. **세션 상태 리셋**: 루프 감지 시 문제 세션 데이터 삭제

## 예상 토큰 절약
이 에러로 삽질 시: 약 30,000~100,000 토큰 소비
이 해결법 참조 시: 약 2,000 토큰

## 출처
Arize AI - Managing Memory in AI Agents
