---
layout: solution
title: "Feature Request: Auto-spawn at context threshold"
category: context-window
source: https://github.com/openclaw/openclaw/issues/13499
description: "Add configuration option to automatically spawn a fresh session when context window exceeds a threshold (e.g.,"
---

# Feature Request: Auto-spawn at context threshold

## 증상
Add configuration option to automatically spawn a fresh session when context window exceeds a threshold (e.g., 50%).

## 원인
Input exceeded the model's maximum context length, causing truncation or a refusal to process the full request. 카테고리: context-window.

## 해결법
Currently using HEARTBEAT.md to check `session_status` and manually spawn when context is high.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/13499
