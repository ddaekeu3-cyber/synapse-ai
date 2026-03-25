---
layout: solution
title: "OpenAI has HALVED paying user's context windows, overnight, without warning."
category: context-window
source: Reddit r/ClaudeAI https://reddit.com/r/OpenAI/comments/1mlif1r/openai_has_halved
---

# OpenAI has HALVED paying user's context windows, overnight, without warning.

## 증상
o3 in the UI supported around **64k** tokens of context, according to community testing.

GPT-5 is clearly listing a hard **32k** context limit in the UI for Plus users. And o3 is no longer available.

So, as a paying customer, you just halved my available context window and called it an upgrade.

Context is the critical element to have productive conversations about code and technical work. It do

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
Reddit r/ClaudeAI https://reddit.com/r/OpenAI/comments/1mlif1r/openai_has_halved_paying_users_context_windows/
