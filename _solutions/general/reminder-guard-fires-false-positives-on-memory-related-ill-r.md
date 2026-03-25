---
layout: solution
title: "Reminder guard fires false positives on memory-related 'I'll remember' statements"
category: general
source: https://github.com/openclaw/openclaw/issues/47586
---

# Reminder guard fires false positives on memory-related "I'll remember" statements

## 증상
The `agent-runner-reminder-guard` appends a visible disclaimer to agent responses when the model says things like "I'll remember" without scheduling a cron job. However, the regex pattern for "remember" is too broad — it matches memory/knowledge retention statements, not just temporal reminder commitments.

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
https://github.com/openclaw/openclaw/issues/47586
