---
layout: solution
title: "Cron jobs with isolated session target timing out"
category: performance
source: https://github.com/openclaw/openclaw/issues/43850
---

# Cron jobs with isolated session target timing out

## 증상
Regression (worked before, now fails)

## 원인
보고된 버그/문제. 카테고리: performance.

## 해결법
Use sessions_spawn from main session instead of cron agentTurn payload for scheduled tasks.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/43850
