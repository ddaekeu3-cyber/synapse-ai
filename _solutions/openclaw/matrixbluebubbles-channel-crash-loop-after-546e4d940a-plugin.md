---
layout: solution
title: "Matrix/BlueBubbles channel crash-loop after 546e4d940a — plugin runtime store split across chunks"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/48477
description: "Regression (worked before, now"
---

# Matrix/BlueBubbles channel crash-loop after 546e4d940a — plugin runtime store split across chunks

## 증상
Regression (worked before, now fails)

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
Roll back to `v2026.3.13-1` (commit `61d171ab0b`), which uses separate tsdown build configs and does not split the runtime store module.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/48477
