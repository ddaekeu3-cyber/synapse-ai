---
layout: solution
title: "Control UI shows incorrect context usage percentage (87%) in Chat tab"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/52591
---

# Control UI shows incorrect context usage percentage (87%) in Chat tab

## 증상
In the Control UI Chat tab, the context usage indicator above the input box sometimes shows an incorrect high percentage (e.g., 87% = 173.8k/200k) while the actual context usage is only ~9% (18k/200k).

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Switching to the Sessions tab and then back to the Chat tab causes the UI to re-render with the correct percentage.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/52591
