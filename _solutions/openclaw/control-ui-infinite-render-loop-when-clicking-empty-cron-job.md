---
layout: solution
title: "Control UI: Infinite render loop when clicking empty cron job name field"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/30208
---

# Control UI: Infinite render loop when clicking empty cron job name field

## 증상
Clicking the "Name" input field in the Cron Jobs tab of the Control UI (without typing anything) triggers an infinite reactive render loop in the browser tab, causing it to consume 99.6% CPU and making the entire dashboard unresponsive.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Kill the stuck browser tab process and reload the dashboard. Avoid clicking the cron name field when it's empty.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/30208
