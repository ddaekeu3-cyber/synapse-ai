---
layout: solution
title: "Cron jobs report status: 'error' with 'Message failed' despite successful Discord delivery"
category: general
source: https://github.com/openclaw/openclaw/issues/50170
---

# Cron jobs report status: "error" with "Message failed" despite successful Discord delivery

## 증상
Regression (worked before, now fails)

## 원인
보고된 버그/문제. 카테고리: general.

## 해결법
None needed for functionality. Messages deliver correctly. The delivered and deliveryStatus fields are accurate. Only the top-level status is misleading, which affects monitoring dashboards and openclaw cron list status display.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/50170
