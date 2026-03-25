---
layout: solution
title: "Background tasks report 'completed' but output files are missing or empty"
category: general
source: https://github.com/anthropics/claude-code/issues/32252
---

# Background tasks report 'completed' but output files are missing or empty

## 증상
Background tasks launched via the Bash tool frequently report as "completed (exit code 0)" in task notifications, but the output file referenced in the notification is either missing from disk or contains 0 bytes. This makes it impossible to verify task results, leading to duplicate work and potential duplicate side effects (e.g., duplicate API calls, duplicate posts to external services).

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
https://github.com/anthropics/claude-code/issues/32252
