---
layout: solution
title: "Lane queue has no task-level timeout — hung promises permanently block session lanes"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/48488
---

# Lane queue has no task-level timeout — hung promises permanently block session lanes

## 증상
Session lanes in the gateway's command queue (`src/process/command-queue.ts`) have no task-level timeout. If an enqueued task's promise never settles, the lane is permanently jammed with no automatic recovery. This affects all messaging channels and cron.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
prioritize the queue level, the API call level, or both?

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/48488
