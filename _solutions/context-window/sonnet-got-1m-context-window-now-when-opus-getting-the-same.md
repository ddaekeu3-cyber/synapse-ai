---
layout: solution
title: "sonnet got 1M context window now, when opus getting the same love??"
category: context-window
source: Reddit r/ClaudeAI https://reddit.com/r/ClaudeAI/comments/1qarks0/sonnet_got_1m_c
---

# sonnet got 1M context window now, when opus getting the same love??

## 증상
just saw this in claude code today and my jaw kinda dropped. sonnet 4.5 sitting there with a 1M context option while opus 4.5 still chilling with the standard context. like dont get me wrong opus is still the big brain model for complex stuff but sometimes i need big brain AND big memory you know?

anyone know if anthropic planning to give opus the 1M treatment too or we just gonna pretend this is

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
Reddit r/ClaudeAI https://reddit.com/r/ClaudeAI/comments/1qarks0/sonnet_got_1m_context_window_now_when_opus/
