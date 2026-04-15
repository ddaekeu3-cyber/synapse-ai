---
layout: solution
title: "Cron schedule object format not accepted (contradicts documentation)"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/50942
description: "Regression (worked before, now"
---

# Cron schedule object format not accepted (contradicts documentation)

## 증상
Regression (worked before, now fails)

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
configuration repeatedly

### Additional information

This issue was discovered while setting up daily learning tasks. The configuration keeps getting converted to object format (possibly by some internal OpenClaw tool/script), causing the cron jobs to fail repeatedly.

Temporary workaround: Use simple format {"schedule": "30 9 * * *"} and regularly check if configuration has been modified.

Suggestions:
1. If object format is not yet implemented, please clarify in documentation that only simple format is currently supported
2. If object format should be supported, please fix the configuration

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/50942
