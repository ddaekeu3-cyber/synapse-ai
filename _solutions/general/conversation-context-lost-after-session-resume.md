---
layout: solution
title: "Conversation context lost after session resume"
category: general
source: https://github.com/anthropics/claude-code/issues/32861
---

# Conversation context lost after session resume

## 증상
세션 재개 시 이전 대화 컨텍스트(디자인 레퍼런스, 레이아웃 논의) 완전 소실. auto-memory는 기술적 사실만 저장하고 디자인/UX 논의는 누락됨. 컨텍스트 압축 시 중요 결정사항 보존 필요.

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
https://github.com/anthropics/claude-code/issues/32861
