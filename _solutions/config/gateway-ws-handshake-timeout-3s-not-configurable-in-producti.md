---
layout: solution
title: "Gateway WS handshake timeout (3s) not configurable in production; closes openclaw acp bridge connections"
category: config
source: https://github.com/openclaw/openclaw/issues/50665
---

# Gateway WS handshake timeout (3s) not configurable in production; closes openclaw acp bridge connections

## 증상
Behavior bug (incorrect output/state without crash)

## 원인
보고된 버그/문제. 카테고리: config.

## 해결법
(maintaining a separate minimal config file) is fragile and breaks if initialization time
  increases for any reason

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/50665
