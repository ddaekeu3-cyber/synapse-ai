---
layout: solution
title: "System prompt runtime stats (usageLine, cacheLine, contextLine) invalidate Anthropic prompt cache on every turn"
category: prompt-engineering
source: https://github.com/openclaw/openclaw/issues/54256
---

# System prompt runtime stats (usageLine, cacheLine, contextLine) invalidate Anthropic prompt cache on every turn

## 증상
`cacheRetention: "long"` is correctly configured and sent to Anthropic, but prompt cache hits only occur within single agentic tool loops. Any new user message or heartbeat event triggers a full cache miss because the system prompt is rebuilt with updated runtime statistics.

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
https://github.com/openclaw/openclaw/issues/54256
