---
layout: solution
title: "Bug: Context token count always shows 0 after compaction"
category: general
source: https://github.com/openclaw/openclaw/issues/50795
---

# Bug: Context token count always shows 0 after compaction

## 증상
The `📚 Context` counter in the TUI always displays `0/1.0m (0%)` even after successful LLM responses. This happens because the `clearStaleAssistantUsageOnSessionMessages()` function incorrectly clears **all** assistant message usage data after any compaction, instead of only clearing usage for messages that existed **before** the compaction.

## 원인
보고된 버그/문제. 카테고리: general.

## 해결법
1. 에러 메시지 정확히 읽기
2. 공식 문서 확인
3. GitHub Issues에서 유사 사례 검색
4. 최소 재현 코드로 원인 격리
5. SynapseAI DB에서 기존 해결법 검색

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/50795
