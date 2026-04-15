---
layout: solution
title: "Cron job timeout/error should send notification via announce delivery"
category: performance
source: https://github.com/openclaw/openclaw/issues/50844
description: "When a cron job times out or fails, the current behavior with delivery mode is completely silent. The job runs, fails/times out, and no notification is"
---

# Cron job timeout/error should send notification via announce delivery

## 증상
When a cron job times out or fails, the current behavior with `announce` delivery mode is completely silent. The job runs, fails/times out, and no notification is sent to the user — they only discover the failure by checking logs or wondering why expected output never arrived.

## 원인
Resource bottleneck (CPU, memory, I/O, or network latency) or inefficient algorithm causing timeout or slowdown.

## 해결법
As a workaround, I've added heartbeat monitoring that checks `cron list` for `consecutiveErrors > 0` and manually notifies the user. But this is not ideal because:
1. It relies on the heartbeat interval (not immediate)
2. It requires custom logic in every agent setup

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/50844
