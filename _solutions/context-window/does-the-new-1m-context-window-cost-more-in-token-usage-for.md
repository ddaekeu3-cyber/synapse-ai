---
layout: solution
title: "Does the new 1M context window cost more in token usage for long Claude Code sessions?"
category: context-window
source: Reddit r/ClaudeAI https://reddit.com/r/ClaudeCode/comments/1rsva0y/does_the_new_
---

# Does the new 1M context window cost more in token usage for long Claude Code sessions?

## 증상
My understanding — and I want to sanity-check this — is that now that the 1M window has gone GA, it doesn't necessarily cost more for the same net amount of activity, and might actually cost *less*.

Here's my reasoning: if you've already consumed 200K tokens in a session and keep going, the previously-used tokens are cached for subsequent requests. So whether you use 900K tokens in one continuous

## 원인
보고된 버그/문제. 카테고리: context-window.

## 해결법
1. 대화 분할: 긴 작업은 여러 세션으로 분리
2. 요약 활용: 이전 대화를 구조화된 요약으로 대체
3. 선택적 컨텍스트: 관련 정보만 포함, 전체 파일 붙여넣기 금지
4. 주기적 리프레시: 20턴마다 컨텍스트 정리
5. 핵심 정보는 프롬프트 시작/끝에 배치

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
Reddit r/ClaudeAI https://reddit.com/r/ClaudeCode/comments/1rsva0y/does_the_new_1m_context_window_cost_more_in_token/
