---
layout: solution
title: "Discord button interactions expire before agent responds"
category: performance
source: https://github.com/openclaw/openclaw/issues/42054
---

# Discord button interactions expire before agent responds

## 증상
Discord component buttons expire with "This component has expired" error before the agent can respond.

## 원인
보고된 버그/문제. 카테고리: performance.

## 해결법
Currently not usable for interactive flows. Text-based responses work fine.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/42054
