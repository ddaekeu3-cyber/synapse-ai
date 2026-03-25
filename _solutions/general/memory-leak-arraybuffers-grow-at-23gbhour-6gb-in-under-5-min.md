---
layout: solution
title: "Memory leak: ArrayBuffers grow at ~23GB/hour, 6GB in under 5 minutes on Windows"
category: general
source: https://github.com/anthropics/claude-code/issues/33437
---

# Memory leak: ArrayBuffers grow at ~23GB/hour, 6GB in under 5 minutes on Windows

## 증상
Claude Code exhibits a severe memory leak on fresh sessions. Memory usage hits 6GB within ~4 minutes of starting, even with no active work being done. The leak is in ArrayBuffers/external memory.

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
https://github.com/anthropics/claude-code/issues/33437
