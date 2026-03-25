---
layout: solution
title: "Feature: Inject context window % into system prompt runtime section"
category: context-window
source: https://github.com/openclaw/openclaw/issues/38568
---

# Feature: Inject context window % into system prompt runtime section

## 증상
The system prompt's **Runtime** section already injects host, OS, model, thinking level, etc. It would be very useful to also inject the current **context window usage percentage** (e.g. `context=49%` or `context=98k/200k (49%)`).

## 원인
보고된 버그/문제. 카테고리: context-window.

## 해결법
Call `session_status` tool every turn and extract the percentage. Works but adds a tool call per turn and relies on the agent remembering to do it.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/38568
