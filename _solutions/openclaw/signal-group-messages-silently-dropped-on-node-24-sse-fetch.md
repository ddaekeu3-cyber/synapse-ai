---
layout: solution
title: "Signal group messages silently dropped on Node 24 — SSE fetch fails with TypeError"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/51716
description: "Regression (worked before, now"
---

# Signal group messages silently dropped on Node 24 — SSE fetch fails with TypeError

## 증상
Regression (worked before, now fails)

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
Adding `NODE_OPTIONS=--no-experimental-fetch` to the container 
environment forces Node to use the legacy http module, which is 
expected to fix the SSE connection.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/51716
