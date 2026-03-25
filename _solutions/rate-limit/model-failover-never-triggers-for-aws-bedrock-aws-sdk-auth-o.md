---
layout: solution
title: "Model failover never triggers for AWS Bedrock (aws-sdk auth) on rate limit"
category: rate-limit
source: https://github.com/openclaw/openclaw/issues/30374
---

# Model failover never triggers for AWS Bedrock (aws-sdk auth) on rate limit

## 증상
When the primary model uses AWS Bedrock (`aws-sdk` auth), rate limit errors never trigger fallback to configured fallback models. The agent loops indefinitely on the rate-limited provider.

## 원인
보고된 버그/문제. 카테고리: rate-limit.

## 해결법
Manual `/model <alias>` switch after hitting rate limit. Gateway restart also clears the stuck state.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/30374
