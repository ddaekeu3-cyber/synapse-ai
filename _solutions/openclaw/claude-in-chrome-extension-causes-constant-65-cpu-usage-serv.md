---
layout: solution
title: "Claude in Chrome extension causes constant 65% CPU usage (service worker loop)"
category: openclaw
source: https://github.com/anthropics/claude-code/issues/37544
---

# Claude in Chrome extension causes constant 65% CPU usage (service worker loop)

## 증상
**Extension:** Claude in Chrome (Beta) v1.0.63

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
None that preserves functionality. Disabling the extension resolves CPU usage but removes all Claude in Chrome features.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/37544
