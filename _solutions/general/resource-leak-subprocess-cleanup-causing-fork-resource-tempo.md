---
layout: solution
title: "Resource leak: subprocess cleanup causing 'fork: Resource temporarily unavailable'"
category: general
source: https://github.com/anthropics/claude-code/issues/35418
---

# Resource leak: subprocess cleanup causing "fork: Resource temporarily unavailable"

## 증상
I frequently start getting `fork: Resource temporarily unavailable' and `vfork: Resource temporarily unavailable` after running claude CLI for a while. I'm guessing that this is due to the many subprocesses it spawns not getting properly cleaned up.

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
https://github.com/anthropics/claude-code/issues/35418
