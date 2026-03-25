---
layout: solution
title: "The most expensive token in AI is the unnecessary one"
category: prompt-engineering
source: Reddit r/ClaudeAI https://reddit.com/r/costlyinfra/comments/1rrgfls/the_most_exp
---

# The most expensive token in AI is the unnecessary one

## 증상
A lot of teams think AI cost optimization is about switching models.

But after looking at multiple AI workloads, the biggest cost drivers usually aren’t the model itself.



They’re things like:



• giant system prompts nobody reads

• RAG context dumps that include entire documents

• multiple model calls per request

• retries when pipelines fail

• GPUs sitting idle between batches



One pro

## 원인
보고된 버그/문제. 카테고리: prompt-engineering.

## 해결법
1. 명확한 지시: 구체적이고 명확한 표현
2. Few-shot 예시: 원하는 출력 예시 제공
3. 역할 지정: 시스템 프롬프트에 역할/제약 명시
4. 출력 포맷 지정: JSON, 마크다운 등
5. 보안: 프롬프트 인젝션 방지 입력 검증

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
Reddit r/ClaudeAI https://reddit.com/r/costlyinfra/comments/1rrgfls/the_most_expensive_token_in_ai_is_the_unnecessary/
