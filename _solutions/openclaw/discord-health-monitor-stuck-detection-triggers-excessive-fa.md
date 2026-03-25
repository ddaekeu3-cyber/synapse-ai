---
layout: solution
title: "Discord: health-monitor 'stuck' detection triggers excessive false-positive reconnects"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/39096
---

# Discord: health-monitor 'stuck' detection triggers excessive false-positive reconnects

## 증상
The built-in Discord health-monitor fires 'stuck' reconnects at an extremely high rate even when the gateway is otherwise healthy — cron jobs running, messages flowing, no WS death spiral in progress.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
None currently — the health-monitor interval/threshold is not configurable.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/39096
