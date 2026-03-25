---
layout: solution
title: "[Feature]: Context Tier Slash Commands"
category: context-window
source: https://github.com/openclaw/openclaw/issues/27925
---

# [Feature]: Context Tier Slash Commands

## 증상
Depending on how personal or aware I need a response to be, the full context (up to 200k tokens) is not necessarily needed in every API call. For simple queries like:

## 원인
보고된 버그/문제. 카테고리: context-window.

## 해결법
was a dual-context set of agents (I am using Claude, and I don't even know this would be possible without two separate API keys / user tokens.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/27925
