---
layout: solution
title: "Add openclaw_agent label to OTEL token metrics"
category: token-cost
source: https://github.com/openclaw/openclaw/issues/24079
---

# Add openclaw_agent label to OTEL token metrics

## 증상
The `openclaw_tokens_total` OTEL metric currently has labels for `openclaw_channel`, `openclaw_model`, `openclaw_provider`, and `openclaw_token` — but **no `openclaw_agent` label** to identify which agent generated the tokens.

## 원인
보고된 버그/문제. 카테고리: token-cost.

## 해결법
Manually calling `sessions_list` and reading `totalTokens` per session. This gives a point-in-time snapshot but no time-series history, and requires custom scripting to push to Prometheus.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/24079
