---
layout: solution
title: "Feature: /model switch should check context size against target model limits"
category: context-window
source: https://github.com/openclaw/openclaw/issues/50619
---

# Feature: /model switch should check context size against target model limits

## 증상
When switching models with `/model` in a session with large accumulated context, the new model may immediately hit rate limits or context overflow.

## 원인
보고된 버그/문제. 카테고리: context-window.

## 해결법
Operational rule: always `/compact` or `/new` before switching to a model with tighter limits.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/50619
