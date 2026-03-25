---
layout: solution
title: "1m context window for opus 4.6 is finally available in claude code"
category: context-window
source: Reddit r/ClaudeAI https://reddit.com/r/ClaudeAI/comments/1r70xa9/1m_context_wind
---

# 1m context window for opus 4.6 is finally available in claude code

## 증상
    $ claude --model=opus[1m]
               Claude Code v2.1.44
     ▐▛███▜▌   Opus 4.6 (1M context) · Claude Max
    ▝▜█████▛▘  /tmp
      ▘▘ ▝▝    Opus 4.6 is here · $50 free extra usage · Try fast mode or use it when you hit a limit /extra-usage to enable
    ❯ Hi!
    ● Hi! How can I help you today?                                                                                               

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
Reddit r/ClaudeAI https://reddit.com/r/ClaudeAI/comments/1r70xa9/1m_context_window_for_opus_46_is_finally/
