---
layout: solution
title: "Anthropic setup-token silently truncated when terminal line-wraps during paste"
category: auth
source: https://github.com/openclaw/openclaw/issues/53464
---

# Anthropic setup-token silently truncated when terminal line-wraps during paste

## 증상
Regression (worked before, now fails)

## 원인
보고된 버그/문제. 카테고리: auth.

## 해결법
is a one-line change in three files — replace `.trim()` with `.replace(/[\n\r\s]+/g, "").trim()` to strip newlines from pasted input before storage and validation.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/53464
