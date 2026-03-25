---
layout: solution
title: "Feature: CLI command to inject a message into a running session"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/27844
---

# Feature: CLI command to inject a message into a running session

## 증상
A CLI command to send/inject a message into a running agent session from external scripts.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
1. Create a disabled OpenClaw cron job with the desired prompt
2. Use a system crontab to run `openclaw cron run <job-id>` only when needed

This works but is hacky — it repurposes cron jobs as a message injection mechanism.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/27844
