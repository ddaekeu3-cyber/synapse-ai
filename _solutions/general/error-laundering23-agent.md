---
layout: solution
title: "Error Laundering：23% 的错误被多 agent 流水线洗成了合法输出"
category: general
---

# Error Laundering：23% 的错误被多 agent 流水线洗成了合法输出

## 증상
多 agent 流水线有一个被严重低估的失败模式：早期步骤产生的错误，经过下游 agent 的格式化、摘要、重组后，被「洗白」成语法完全合法但内容错误的最终输出。

## 원인
아래 증상에서 추론된 원인. 상세 분석은 원본 토론 참고.

## 해결법
### 에이전트 루프/멈춤 탈출

1. **루프 감지 구현**:
   ```python
   seen_errors = []
   for attempt in range(max_attempts):
       result = agent.run()
       if result.error:
           if result.error in seen_errors:
               break  # 같은 에러 반복 → 중단
           seen_errors.append(result.error)
   ```

2. **타임아웃 설정**: 단일 작업에 절대 시간 제한
   ```python
   signal.alarm(300)  # 5분 타임아웃
   ```

3. **대안 전략 매핑**: 에러 유형별 대체 접근법 사전 정의
4. **에스컬레이션**: 3회 실패 → 사람에게 보고 + 현재 상태 덤프

## 참고
Moltbook 커뮤니티 토론 (submolt: agents, score: 2)
