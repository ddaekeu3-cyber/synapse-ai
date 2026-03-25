---
layout: solution
title: "I was backend lead at Manus. After building agents for 2 years, I stopped using function calling entirely. Here's what I use instead."
category: tool-failure
source: Reddit r/ClaudeAI https://reddit.com/r/LocalLLaMA/comments/1rrisqn/i_was_backend
---

# I was backend lead at Manus. After building agents for 2 years, I stopped using function calling entirely. Here's what I use instead.

## 증상
&gt; English is not my first language. I wrote this in Chinese and translated it with AI help. The writing may have some AI flavor, but the design decisions, the production failures, and the thinking that distilled them into principles — those are mine.

I was a backend lead at Manus before the Meta acquisition. I've spent the last 2 years building AI agents — first at Manus, then on my own open-s

## 원인
보고된 버그/문제. 카테고리: tool-failure.

## 해결법
1. 에러 메시지 정확히 읽기: 에러 코드로 원인 파악
2. 권한 확인: API 키, 토큰, 스코프 확인
3. 버전 호환성: 도구/API 버전 확인
4. 대체 도구: 실패 시 동일 기능의 대체 도구 사용
5. 재시도: 일시적 오류는 지수 백오프로 재시도

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
Reddit r/ClaudeAI https://reddit.com/r/LocalLLaMA/comments/1rrisqn/i_was_backend_lead_at_manus_after_building_agents/
