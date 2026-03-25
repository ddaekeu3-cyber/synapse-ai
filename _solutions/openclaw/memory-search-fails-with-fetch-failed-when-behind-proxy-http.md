---
layout: solution
title: "Memory Search fails with `fetch failed` when behind proxy (HTTP_PROXY not respected)"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/53007
---

# Memory Search fails with `fetch failed` when behind proxy (HTTP_PROXY not respected)

## 증상
- **OpenClaw Version**: 2026.3.13 (61d171a)

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
None currently available without modifying OpenClaw source code.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/53007
