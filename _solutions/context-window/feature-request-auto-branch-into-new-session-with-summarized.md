---
layout: solution
title: "Feature Request: Auto-branch into new session with summarized context when context limit is reached instead of forcing to /clear"
category: context-window
source: https://github.com/anthropics/claude-code/issues/25695
---

# Feature Request: Auto-branch into new session with summarized context when context limit is reached instead of forcing to /clear

## 증상
When a Claude Code session hits its maximum context window, the session effectively dies. Compaction can only compress so much - once you've compacted multiple times and the summary itself fills the window, you're stuck. The only option is to start a fresh session and manually re-explain everything from scratch. This is the single most frustrating experience in Claude Code.

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
https://github.com/anthropics/claude-code/issues/25695
