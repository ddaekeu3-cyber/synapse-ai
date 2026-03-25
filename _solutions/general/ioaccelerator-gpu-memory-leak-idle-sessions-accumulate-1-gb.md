---
layout: solution
title: "IOAccelerator GPU memory leak: idle sessions accumulate ~1 GB non-reclaimable footprint each"
category: general
source: https://github.com/anthropics/claude-code/issues/35804
---

# IOAccelerator GPU memory leak: idle sessions accumulate ~1 GB non-reclaimable footprint each

## 증상
Long-lived Claude Code sessions accumulate 700–968 MB of non-reclaimable IOAccelerator (GPU) dirty pages that are never freed. This is invisible to RSS monitoring (7 MB RSS vs 1.3 GB footprint — 15× underestimate) but triggers macOS "out of memory" warnings attributed to the parent terminal. On a 32 GB machine with 37 idle sessions, this totals ~41 GB of memory pressure from GPU buffers alone.

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
https://github.com/anthropics/claude-code/issues/35804
