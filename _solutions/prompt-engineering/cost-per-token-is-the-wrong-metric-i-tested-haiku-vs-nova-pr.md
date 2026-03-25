---
layout: solution
title: "Cost per token is the wrong metric. I tested Haiku vs Nova Pro vs Nova Lite with identical RAG pipelines and the cheapest model per token was the most expensive per useful answer"
category: prompt-engineering
source: Reddit r/ClaudeAI https://reddit.com/r/ClaudeAI/comments/1rj2fwv/cost_per_token_
---

# Cost per token is the wrong metric. I tested Haiku vs Nova Pro vs Nova Lite with identical RAG pipelines and the cheapest model per token was the most expensive per useful answer

## 증상
I ran an experiment this weekend comparing Claude Haiku 4.5, Amazon Nova Pro, and Amazon Nova Lite on the same RAG pipeline. Not synthetic benchmarks — a production chatbot with real queries.

**Setup:**

* Two vector stores (product docs + marketing/competitive docs)
* 13 ADRs (Architecture Decision Records) as grounding context
* \~49K input tokens of retrieved context per query
* Same system pr

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
Reddit r/ClaudeAI https://reddit.com/r/ClaudeAI/comments/1rj2fwv/cost_per_token_is_the_wrong_metric_i_tested_haiku/
