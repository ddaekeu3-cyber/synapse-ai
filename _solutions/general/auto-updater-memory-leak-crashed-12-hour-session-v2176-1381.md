---
layout: solution
title: "Auto-updater memory leak crashed 12-hour session (v2.1.76) — 13.81 GB committed, Bun panic"
category: general
source: https://github.com/anthropics/claude-code/issues/35171
---

# Auto-updater memory leak crashed 12-hour session (v2.1.76) — 13.81 GB committed, Bun panic

## 증상
Claude Code v2.1.76 crashed overnight with a Bun panic ("Illegal instruction") after accumulating **13.81 GB of committed memory** during a 12-hour session. The crash appears related to the auto-updater memory leak that was fixed in v2.1.77:

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
https://github.com/anthropics/claude-code/issues/35171
