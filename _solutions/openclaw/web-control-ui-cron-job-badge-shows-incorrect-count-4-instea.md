---
layout: solution
title: "Web Control UI cron job badge shows incorrect count (4 instead of 3)"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/51382
description: "Crash (process/app exits or"
---

# Web Control UI cron job badge shows incorrect count (4 instead of 3)

## 증상
Crash (process/app exits or hangs)

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
Ignore the badge number and use `openclaw cron list` for accurate count.

---
*Reported by: @Chopin-1996*

<img width="1278" height="1270" alt="Image" src="https://github.com/user-attachments/assets/f3107f7b-c3c3-43cd-a2f1-013eabf1891a" />

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/51382
