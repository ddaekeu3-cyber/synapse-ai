---
layout: solution
title: "Feature Request: Stream Repetition Safeguard (Halt & Confirm)"
category: loop-stuck
source: https://github.com/openclaw/openclaw/issues/44965
description: "When using certain models, the generation can occasionally get stuck in an infinite loop, spamming the exact same phrase (e.g., \"Done, I will output it\")"
---

# Feature Request: Stream Repetition Safeguard (Halt & Confirm)

## 증상
When using certain models, the generation can occasionally get stuck in an infinite loop, spamming the exact same phrase (e.g., "Done, I will output it") endlessly. This floods chat channels with identical lines of text and wastes tokens.

## 원인
Agent entered a retry or decision loop without an exit condition, consuming tokens indefinitely without making progress. 카테고리: loop-stuck.

## 해결법
you'd like
Implement a stream-level repetition safeguard in the core daemon's streaming handler. 
1. Maintain a sliding window/buffer of recent stream chunks.
2. Count the number of sequential identical responses/phrases.
3. If the count exceeds a threshold (e.g., 20 repetitions), **halt the LLM stream immediately**.
4. Pause execution and send an alert to the user instead of the spammed text (e.g., "⚠️ Output paused: Detected 20 repeating sequences.").
5. Require manual confirmation from the user (e.g., `/approve` to resume or `/cancel` to abort) before continuing.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/44965
