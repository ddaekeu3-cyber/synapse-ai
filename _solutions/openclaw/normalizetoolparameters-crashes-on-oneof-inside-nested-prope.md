---
layout: solution
title: "normalizeToolParameters crashes on oneOf inside nested property definitions"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/27652
---

# normalizeToolParameters crashes on oneOf inside nested property definitions

## 증상
`normalizeToolParameters` crashes with `Cannot read properties of undefined (reading 'properties')` when a plugin tool schema uses `oneOf` inside a nested property definition.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Replace `oneOf` with a flat object schema (all fields optional). Functionally equivalent for the handler code.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/27652
