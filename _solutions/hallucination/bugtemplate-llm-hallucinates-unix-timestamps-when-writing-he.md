---
layout: solution
title: "[Bug/Template]: LLM hallucinates Unix timestamps when writing heartbeat-state.json — causes skipped or over-triggered checks"
category: hallucination
source: https://github.com/openclaw/openclaw/issues/49086
description: "The AGENTS.md template recommends tracking heartbeat checks in with Unix timestamps. However, LLMs frequently hallucinate incorrect epoch values when"
---

# [Bug/Template]: LLM hallucinates Unix timestamps when writing heartbeat-state.json — causes skipped or over-triggered checks

## 증상
The AGENTS.md template recommends tracking heartbeat checks in `memory/heartbeat-state.json` with Unix timestamps. However, LLMs frequently **hallucinate incorrect epoch values** when writing these timestamps, causing heartbeat checks to be skipped entirely or over-triggered.

## 원인
Model generated plausible but incorrect output due to insufficient grounding, missing verification, or high sampling temperature.

## 해결법
We created a wrapper script (`heartbeat-state.py`) that abstracts all timestamp operations:

```bash

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/49086
