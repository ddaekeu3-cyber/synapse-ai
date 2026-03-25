---
layout: solution
title: "Show status line during permission prompts and user questions"
category: general
source: https://github.com/anthropics/claude-code/issues/30232
---

# Show status line during permission prompts and user questions

## 증상
The status line is currently hidden during permission prompts and `AskUserQuestion` interactions. It would be valuable to keep the status line visible during these moments.

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
https://github.com/anthropics/claude-code/issues/30232
