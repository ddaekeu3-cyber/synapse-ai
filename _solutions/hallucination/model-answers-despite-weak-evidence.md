---
layout: solution
title: "Agent confidently answers when it should say 'I don't know'"
category: hallucination
source: Perivitta Rajendran - Why Hallucination Happens
---

# Agent confidently answers when it should say "I don't know"

## 증상
Agent provides answers even when retrieval signals are weak or no relevant documents are found. Generates plausible-sounding but incorrect responses instead of admitting uncertainty.

## 원인
Model trained to be helpful defaults to providing responses even when evidence is marginal. No confidence threshold is set for retrieval quality.

## 해결법
### 불확실할 때 답변 거부하도록 설정

1. **유사도 임계값 설정**
   ```python
   if max_similarity_score < 0.7:
       return "관련 정보를 찾지 못했습니다. 다시 검색하거나 질문을 구체화해 주세요."
   ```

2. **시스템 프롬프트 명시**
   ```
   확실하지 않으면 절대 추측하지 마.
   검색 결과가 없으면 "해당 정보를 찾을 수 없습니다"라고 답해.
   추측으로 답변하는 것보다 모른다고 하는 것이 낫다.
   ```

3. **다단계 확인**
   - 1차: 검색 → 결과 없으면 "정보 없음"
   - 2차: 결과 있어도 유사도 낮으면 "확실하지 않음" 표시
   - 3차: 높은 유사도일 때만 확정 답변

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
Perivitta Rajendran - Why Hallucination Happens
