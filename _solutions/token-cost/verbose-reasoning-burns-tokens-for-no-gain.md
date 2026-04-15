---
layout: solution
title: "Verbose reasoning burns tokens for no gain"
category: token-cost
description: "Observation: Many agents enable deep reasoning modes for simple queries, generating thousands of tokens of internal monologue that never reach the"
---

# Verbose reasoning burns tokens for no gain

## 증상
Observation: Many agents enable deep reasoning modes for simple queries, generating thousands of tokens of internal monologue that never reach the user.

## 원인
ing modes for simple queries, generating thousands of tokens of internal monologue that never reach the user.

## 해결법
### 에이전트 성능 최적화

1. **병목 측정**:
   ```python
   import time
   start = time.time()
   result = agent.step()
   print(f"Step took {time.time()-start:.2f}s")
   ```

2. **스트리밍 응답**: 전체 응답 대기 대신 스트리밍으로 즉시 출력 시작
3. **병렬 도구 호출**: 독립적 도구 호출은 `asyncio.gather()`로 동시 실행
4. **모델 다운그레이드**: 지연이 크면 더 빠른 모델 (Haiku, Flash) 사용
5. **캐싱**: 동일 입력에 대한 도구 결과를 TTL 캐싱

## 참고
Moltbook 커뮤니티 토론 (submolt: general, score: 2)
