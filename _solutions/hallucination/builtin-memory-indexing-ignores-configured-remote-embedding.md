---
layout: solution
title: "Builtin memory indexing ignores configured remote embedding batch timeout"
category: hallucination
source: https://github.com/openclaw/openclaw/issues/49933
---

# Builtin memory indexing ignores configured remote embedding batch timeout

## 증상
Behavior bug (incorrect output/state without crash)

## 원인
보고된 버그/문제. 카테고리: hallucination.

## 해결법
ed 120 second ceiling.

### OpenClaw version

2026.3.13 tested as stable baseline, with a local patch applied for verification

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/49933
