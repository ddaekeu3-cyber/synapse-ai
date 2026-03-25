---
layout: solution
title: "Feature: Activity-aware run timeout (wall-clock timeout kills active multi-tool runs)"
category: tool-failure
source: https://github.com/openclaw/openclaw/issues/41588
---

# Feature: Activity-aware run timeout (wall-clock timeout kills active multi-tool runs)

## 증상
The gateway run timeout (`agents.defaults.timeoutSeconds`, default 600s) is a flat wall-clock timer. It kills runs that exceed the limit regardless of whether they are actively making progress.

## 원인
보고된 버그/문제. 카테고리: tool-failure.

## 해결법
Bump `agents.defaults.timeoutSeconds` to a higher value (e.g. 1200). This is a band-aid — long research sessions can still hit it.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/41588
