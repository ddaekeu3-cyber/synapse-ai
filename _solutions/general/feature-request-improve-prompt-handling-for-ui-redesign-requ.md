---
layout: solution
title: "[Feature Request] Improve prompt handling for UI redesign requests and reduce overconfident language in Claude responses"
category: general
source: https://github.com/anthropics/claude-code/issues/32719
---

# [Feature Request] Improve prompt handling for UI redesign requests and reduce overconfident language in Claude responses

## 증상
Opus get the UI incorrectly, and could not fix it comprehensively (which is okay, just need to prompt it better)

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
https://github.com/anthropics/claude-code/issues/32719
