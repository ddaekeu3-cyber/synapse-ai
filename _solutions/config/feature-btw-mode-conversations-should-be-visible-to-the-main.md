---
layout: solution
title: "[FEATURE] BTW mode conversations should be visible to the main conversation context"
category: config
source: https://github.com/anthropics/claude-code/issues/35940
---

# [FEATURE] BTW mode conversations should be visible to the main conversation context

## 증상
BTW mode is great for quick side questions during complex tasks, but its conversations are completely ephemeral — they're not accessible to the main conversation context after dismissal. This creates a practical issue:

## 원인
보고된 버그/문제. 카테고리: config.

## 해결법
Tell the main agent "remember: [thing from BTW]" — but this requires the user to manually relay information, which is exactly the friction BTW mode is supposed to eliminate.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/35940
