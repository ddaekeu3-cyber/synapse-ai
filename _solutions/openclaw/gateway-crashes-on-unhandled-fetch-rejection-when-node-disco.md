---
layout: solution
title: "Gateway crashes on unhandled fetch rejection when node disconnects"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/34006
---

# Gateway crashes on unhandled fetch rejection when node disconnects

## 증상
The gateway crashes with an unhandled promise rejection when a node disconnects (e.g., during reboot).

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Manually restart the gateway after crash.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/34006
