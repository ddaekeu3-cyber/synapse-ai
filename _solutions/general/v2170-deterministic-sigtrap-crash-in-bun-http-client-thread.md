---
layout: solution
title: "v2.1.70: Deterministic SIGTRAP crash in Bun HTTP Client thread (3 crashes, identical stack)"
category: general
source: https://github.com/anthropics/claude-code/issues/31524
---

# v2.1.70: Deterministic SIGTRAP crash in Bun HTTP Client thread (3 crashes, identical stack)

## 증상
Claude Code v2.1.70 crashes with `EXC_BREAKPOINT/SIGTRAP` on the Bun "HTTP Client" thread. This is a **deterministic** bug — all 3 crashes have an **identical call stack** and crash at the same offset (`0x3220190`). No crashes were observed with v2.1.68 or v2.1.69.

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
https://github.com/anthropics/claude-code/issues/31524
