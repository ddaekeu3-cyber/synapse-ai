---
layout: solution
title: "Feature Request: Per-model usage logging for cost tracking"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/13219
---

# Feature Request: Per-model usage logging for cost tracking

## 증상
Request native per-model usage logging to enable cost tracking and model-mix optimization.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
I've built `session-usage-report.py` that parses session JSONL files, but it's slow for large session histories and doesn't capture cron job isolation sessions well.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/13219
