---
layout: solution
title: "bug: heartbeat interval collapses to 5-8 min despite configured 90m"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/27807
---

# bug: heartbeat interval collapses to 5-8 min despite configured 90m

## 증상
Heartbeat configured with `every: "90m"` starts firing every 5-8 minutes after running correctly for several hours. The interval progressively shortens over time.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Gateway restart temporarily fixes the interval, but it eventually collapses again.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/27807
