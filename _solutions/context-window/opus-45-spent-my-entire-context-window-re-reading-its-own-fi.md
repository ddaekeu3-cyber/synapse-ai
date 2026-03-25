---
layout: solution
title: "Opus 4.5 spent my entire context window re-reading its own files before doing anything. Full day lost. Zero output."
category: context-window
source: Reddit r/ClaudeAI https://reddit.com/r/ClaudeAI/comments/1quhglx/opus_45_spent_m
---

# Opus 4.5 spent my entire context window re-reading its own files before doing anything. Full day lost. Zero output.

## 증상
**Yesterday I burned a full day trying to get Opus 4.5 through complex tasks. What I actually got was a masterclass in recursive self-destruction.**

The pattern is always the same. You give it a real task. It starts reading its skill files. Reads them again. Decides it needs to check something else. Rereads the first file "just to be sure." Starts processing. Rereads. The context window fills up 

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
Reddit r/ClaudeAI https://reddit.com/r/ClaudeAI/comments/1quhglx/opus_45_spent_my_entire_context_window_rereading/
