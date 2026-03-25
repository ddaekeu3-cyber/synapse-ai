---
layout: solution
title: "Memory leak in native addons: ~18 GB/hour growth, RSS reaches 2 GB in 7 minutes"
category: general
source: https://github.com/anthropics/claude-code/issues/32752
---

# Memory leak in native addons: ~18 GB/hour growth, RSS reaches 2 GB in 7 minutes

## 증상
Claude Code CLI process exhibits aggressive memory growth (~18 GB/hour), reaching ~2 GB RSS in under 7 minutes. The diagnostics indicate the leak is in native memory (outside V8 heap), likely in native addons such as `node-pty`.

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
https://github.com/anthropics/claude-code/issues/32752
