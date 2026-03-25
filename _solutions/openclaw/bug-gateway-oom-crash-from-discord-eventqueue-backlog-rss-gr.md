---
layout: solution
title: "Bug: Gateway OOM crash from Discord EventQueue backlog (RSS grows to 4GB)"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/30212
---

# Bug: Gateway OOM crash from Discord EventQueue backlog (RSS grows to 4GB)

## 증상
Gateway process RSS grows continuously when Discord EventQueue has sustained backlog, eventually hitting the Node.js heap limit (~4GB) and crashing with `FATAL ERROR: Ineffective mark-compacts near heap limit Allocation failed - JavaScript heap out of memory`.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Periodic `openclaw gateway restart` (every 4-6 hours) to clear the EventQueue and reset RSS.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/30212
