---
layout: solution
title: "Config validation fails when auth profile references removed provider"
category: auth
source: https://github.com/openclaw/openclaw/issues/38913
---

# Config validation fails when auth profile references removed provider

## 증상
Repro (OpenClaw 2026.3.2 on macOS):

## 원인
보고된 버그/문제. 카테고리: auth.

## 해결법
- Manually delete the orphaned entry under auth.profiles.

Happy to provide sanitized config snippet if needed.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/38913
