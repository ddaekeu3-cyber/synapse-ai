---
layout: solution
title: "Official: Anthropic just released Claude Code 2.1.14 with 16 CLI, 5 flag and 4 prompt changes, details below"
category: context-window
source: Reddit r/ClaudeAI https://reddit.com/r/ClaudeAI/comments/1qj9g2a/official_anthro
---

# Official: Anthropic just released Claude Code 2.1.14 with 16 CLI, 5 flag and 4 prompt changes, details below

## 증상
**Claude Code CLI 2.1.14 Changelog:**

• Added history-based autocomplete in **bash** mode (`!`) - type a partial command and press Tab to complete from your bash command history.

• Added **search** to installed plugins list - type to filter by name or description.

• Added **support** for pinning plugins to specific git commit SHAs, allowing marketplace entries to install exact versions.

• Fixe

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
Reddit r/ClaudeAI https://reddit.com/r/ClaudeAI/comments/1qj9g2a/official_anthropic_just_released_claude_code_2114/
