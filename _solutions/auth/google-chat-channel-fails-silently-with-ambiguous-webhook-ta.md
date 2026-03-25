---
layout: solution
title: "Google Chat channel fails silently with 'ambiguous webhook target' 401"
category: auth
source: https://github.com/openclaw/openclaw/issues/22362
---

# Google Chat channel fails silently with 'ambiguous webhook target' 401

## 증상
- **OpenClaw version:** 2026.2.19-2

## 원인
보고된 버그/문제. 카테고리: auth.

## 해결법
Currently none — channel fails to initialize regardless of config changes.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/22362
