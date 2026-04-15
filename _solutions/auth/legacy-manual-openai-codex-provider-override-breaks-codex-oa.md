---
layout: solution
title: "Legacy manual openai-codex provider override breaks Codex OAuth after #37558 / #38026; doctor should detect and fix"
category: auth
source: https://github.com/openclaw/openclaw/issues/40066
description: "Users who manually configured before the recent Codex OAuth fixes can remain broken even after upgrading to a version that"
---

# Legacy manual openai-codex provider override breaks Codex OAuth after #37558 / #38026; doctor should detect and fix

## 증상
Users who manually configured `models.providers.openai-codex` before the recent Codex OAuth fixes can remain broken **even after upgrading** to a version that includes:

## 원인
Authentication credential mismatch, expiry, or permission scope gap between the requesting agent and the target API.

## 해결법
Remove the manual `models.providers.openai-codex` override from config and restart OpenClaw.

In our case, that was the key step that made `openai-codex/gpt-5.4` start working.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/40066
