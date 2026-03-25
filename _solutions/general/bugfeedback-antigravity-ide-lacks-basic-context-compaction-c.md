---
layout: solution
title: "[Bug/Feedback] Antigravity IDE lacks basic context compaction. Claude Opus 4.6 is virtually UNUSABLE due to constant 'prompt is too long' agent terminations."
category: general
source: Reddit r/ClaudeAI https://reddit.com/r/google_antigravity/comments/1rjifwb/bugfe
---

# [Bug/Feedback] Antigravity IDE lacks basic context compaction. Claude Opus 4.6 is virtually UNUSABLE due to constant "prompt is too long" agent terminations.

## 증상
I am consistently encountering fatal agent terminations when using Claude Opus 4.6 (Thinking) on Antigravity IDE. Instead of managing context gracefully, the agent simply terminates midway through any moderately complex session.

**Here is the exact Debug Info I keep seeing:**

* Error: `HTTP 400 Bad Request`
* Detail: `prompt is too long: 218849 tokens &gt; 200000 maximum`

**The Core Architectur

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
Reddit r/ClaudeAI https://reddit.com/r/google_antigravity/comments/1rjifwb/bugfeedback_antigravity_ide_lacks_basic_context/
