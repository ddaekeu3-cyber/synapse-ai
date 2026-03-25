---
layout: solution
title: "1 million context window is now generally available for Claude Opus 4.6 and Claude Sonnet 4.6."
category: context-window
source: Reddit r/ClaudeAI https://reddit.com/r/ClaudeAI/comments/1rsubm0/1_million_conte
---

# 1 million context window is now generally available for Claude Opus 4.6 and Claude Sonnet 4.6.

## 증상
Claude Opus 4.6 and Sonnet 4.6 now include the full 1M context window at standard pricing on the Claude Platform.

Opus 4.6 scores 78.3% on MRCR v2 at 1 million tokens, highest among frontier models.

Load entire codebases, large document sets, and long-running agents. Media limits expand to 600 images or PDF pages per request. 

Now available on all plans and by default on Claude Code.

  
Learn 

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
Reddit r/ClaudeAI https://reddit.com/r/ClaudeAI/comments/1rsubm0/1_million_context_window_is_now_generally/
