---
layout: solution
title: "Agentic planner fails to complete full task execution and reports gaps after claiming completion"
category: general
source: https://github.com/anthropics/claude-code/issues/38531
---

# Agentic planner fails to complete full task execution and reports gaps after claiming completion

## 증상
Write a plan, it exutes the plan, it says finished the plan, you say did you complete the whole plan, it says no here are all the gaps.....if there is a plan it need to 100% reliabily finish the whole plan. If it can't be done in 1 it needs to tell you that up front.

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
https://github.com/anthropics/claude-code/issues/38531
