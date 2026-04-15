---
layout: solution
title: "Heartbeat ignores lightContext: true, loads full agent context + unbounded session history"
category: context-window
source: https://github.com/openclaw/openclaw/issues/43767
description: "Regression (worked before, now"
---

# Heartbeat ignores lightContext: true, loads full agent context + unbounded session history

## 증상
Regression (worked before, now fails)

## 원인
Input exceeded the model's maximum context length, causing truncation or a refusal to process the full request. 카테고리: context-window.

## 해결법
Set `heartbeat.every` to `"off"` in `openclaw.json` to disable heartbeats entirely.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/43767
