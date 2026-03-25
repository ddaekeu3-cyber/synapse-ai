---
layout: solution
title: "Cut hallucination by half with pre-output prompt injection"
category: prompt-engineering
source: Reddit r/ClaudeAI https://reddit.com/r/ClaudeAI/comments/1rz9i7n/cut_hallucinati
---

# Cut hallucination by half with pre-output prompt injection

## 증상
**1. In your Claude Code output style (system prompt)**, add:

````markdown
## Before response

IMPORTANT: MUST run before responding to user, including follow-ups. NO EXCEPTIONS.

```
python -m pre_output.record '{
  "turn": 1/2/...,
  "summary": "10 words max",
  "uncertainties": ["unresolved observations, unverified assumptions", ...],
  "possible-next-steps": ["refactor, update docs", ...]
}'


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
Reddit r/ClaudeAI https://reddit.com/r/ClaudeAI/comments/1rz9i7n/cut_hallucination_by_half_with_preoutput_prompt/
