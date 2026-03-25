---
layout: solution
title: "Gemini CLI OAuth fails with 400 Bad Request: loadCodeAssist failed"
category: auth
source: https://github.com/openclaw/openclaw/issues/44858
---

# Gemini CLI OAuth fails with 400 Bad Request: loadCodeAssist failed

## 증상
- **OpenClaw Version**: 2026.3.11 (29dc654)

## 원인
보고된 버그/문제. 카테고리: auth.

## 해결법
Currently using `kimi-coding/k2p5` as the primary model to avoid dependency on Gemini OAuth.

---
**Reporter**: @test
**Date**: 2026-03-13

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/44858
