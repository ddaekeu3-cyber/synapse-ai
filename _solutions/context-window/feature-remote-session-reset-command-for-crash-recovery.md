---
layout: solution
title: "Feature: Remote session reset command for crash recovery"
category: context-window
source: https://github.com/openclaw/openclaw/issues/9272
---

# Feature: Remote session reset command for crash recovery

## 증상
When a session crashes due to context overflow (e.g., prompt exceeds model limits), the gateway becomes unresponsive for that session. The only recovery path currently requires:

## 원인
보고된 버그/문제. 카테고리: context-window.

## 해결법
things is broken because the session behind it is crashed.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/9272
