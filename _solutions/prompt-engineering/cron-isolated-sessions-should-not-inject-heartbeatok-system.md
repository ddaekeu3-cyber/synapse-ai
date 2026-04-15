---
layout: solution
title: "Cron isolated sessions should not inject HEARTBEAT_OK system prompt instructions"
category: prompt-engineering
source: https://github.com/openclaw/openclaw/issues/43274
description: "Cron jobs with inherit the full system prompt, including the heartbeat"
---

# Cron isolated sessions should not inject HEARTBEAT_OK system prompt instructions

## 증상
Cron jobs with `sessionTarget: "isolated"` inherit the full system prompt, including the heartbeat instruction:

## 원인
Prompt structure conflict or ambiguous instruction caused the model to misinterpret the intended task. 카테고리: prompt-engineering.

## 해결법
We added explicit "forbidden words" in the cron prompt and switched to a two-step architecture (write report to file first, then output). But this is fragile — it depends on model compliance with prompt instructions.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/43274
