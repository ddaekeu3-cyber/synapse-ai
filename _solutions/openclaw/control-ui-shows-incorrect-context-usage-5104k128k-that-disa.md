---
layout: solution
title: "Control UI shows incorrect context usage (510.4k/128k) that disappears after page refresh"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/50196
---

# Control UI shows incorrect context usage (510.4k/128k) that disappears after page refresh

## 증상
Behavior bug (incorrect output/state without crash)

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
refresh)
**Consequence**: Users may incorrectly think context is full when it's not, potentially leading to unnecessary session management actions. The workaround (refresh) is simple but inconvenient.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/50196
