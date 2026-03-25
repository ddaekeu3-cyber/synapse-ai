---
layout: solution
title: "Context window not enforced on model switch — oversized prompts sent to smaller models"
category: context-window
source: https://github.com/openclaw/openclaw/issues/51638
---

# Context window not enforced on model switch — oversized prompts sent to smaller models

## 증상
When a session accumulates context on a large-window model (e.g., Sonnet at 200K) and then switches to a smaller-window model (e.g., Qwen 3.5 35B at 32K), OpenClaw sends the full accumulated context to the new model without re-compacting. This causes the downstream inference server to OOM.

## 원인
보고된 버그/문제. 카테고리: context-window.

## 해결법
- Removed Qwen model aliases from the agent to prevent model switching
- Keeping Bravo's MLX server unloaded until fix is confirmed

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/51638
