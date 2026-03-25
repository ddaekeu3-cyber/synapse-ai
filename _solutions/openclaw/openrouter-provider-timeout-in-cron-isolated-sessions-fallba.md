---
layout: solution
title: "OpenRouter provider timeout in cron isolated sessions - fallback not triggered"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/49597
---

# OpenRouter provider timeout in cron isolated sessions - fallback not triggered

## 증상
When using `openrouter/hunter-alpha` as the model for cron jobs with `sessionTarget: "isolated"`, the provider consistently times out (~125s) with "Provider returned error", but the configured model fallback (`ollama/glm-5:cloud`) is never triggered.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Use `ollama/glm-5:cloud` for cron jobs (local, no network dependency).

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/49597
