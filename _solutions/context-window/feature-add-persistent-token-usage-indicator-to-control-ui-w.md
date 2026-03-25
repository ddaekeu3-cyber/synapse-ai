---
layout: solution
title: "[Feature]: Add persistent token usage indicator to Control UI / WebChat"
category: context-window
source: https://github.com/openclaw/openclaw/issues/46398
---

# [Feature]: Add persistent token usage indicator to Control UI / WebChat

## 증상
When using Ollama models with limited context windows, users have no way to monitor token usage in real-time from the chat UI. The context can suddenly exceed the max limit, causing errors or unexpected behavior.

## 원인
보고된 버그/문제. 카테고리: context-window.

## 해결법
- Manually running `session_status` via the CLI or asking the assistant
- Running `openclaw chat` in a separate terminal for live token tracking

These work but aren't ideal for users who only use the web UI.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/46398
