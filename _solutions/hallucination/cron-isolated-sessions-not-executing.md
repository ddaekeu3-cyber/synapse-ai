---
layout: solution
title: "Cron: Isolated sessions not executing"
category: hallucination
source: https://github.com/openclaw/openclaw/issues/52806
description: "Behavior bug (incorrect output/state without"
---

# Cron: Isolated sessions not executing

## 증상
Behavior bug (incorrect output/state without crash)

## 원인
Model generated plausible but incorrect output due to insufficient grounding, missing verification, or high sampling temperature.

## 해결법
Using manual execution - user requests "执行日报" which triggers direct tool calls instead of cron.

Evidence that isolated sessions CAN work:
The job lobster-daily-report (Job ID: 723d3ba3-e912-4956-b215-3dfbdd3b6b4d) with identical configuration (isolated + agentTurn) DOES execute successfully at 12:00 daily. This suggests a race condition or state corruption issue affecting other jobs.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/52806
