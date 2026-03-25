---
layout: solution
title: "Feature: Slack plugin — subscribe to canvas_updated events"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/40882
---

# Feature: Slack plugin — subscribe to canvas_updated events

## 증상
The Slack plugin currently only subscribes to `message` events (Socket Mode). Slack also provides a `canvas_updated` event that fires in real-time whenever a Canvas document is edited.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Polling via heartbeat/cron (inefficient, adds lag, burns tokens).

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/40882
