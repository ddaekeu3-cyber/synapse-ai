---
layout: solution
title: "Introducing Pommel - an open source tool to help Claude Code find code without burning your context window"
category: context-window
source: Reddit r/ClaudeAI https://reddit.com/r/ClaudeAI/comments/1q0gkn8/introducing_pom
---

# Introducing Pommel - an open source tool to help Claude Code find code without burning your context window

## 증상
Update:  Thanks for the strong response and questions!  Based on some of your feedback, I've just released 0.5.x, which adds a bunch of new features and provides the ability to do metrics so you can tell how much context you're really saving.  Check out the follow up post [here](https://www.reddit.com/r/ClaudeAI/comments/1q1hssl/pommel_v050_hybrid_search_benchmarks_and/)!



I kept hitting the sam

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
Reddit r/ClaudeAI https://reddit.com/r/ClaudeAI/comments/1q0gkn8/introducing_pommel_an_open_source_tool_to_help/
