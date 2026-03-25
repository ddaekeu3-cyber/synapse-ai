---
layout: solution
title: "Gateway peak memory 6GB in 26 minutes of runtime"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/24689
---

# Gateway peak memory 6GB in 26 minutes of runtime

## 증상
- **OpenClaw version:** 2026.2.22-2

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Added `MemoryHigh=1536M` and `MemoryMax=2G` to the systemd unit.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/24689
