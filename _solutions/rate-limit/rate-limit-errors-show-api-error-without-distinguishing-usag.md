---
layout: solution
title: "Rate limit errors show 'API Error' without distinguishing usage vs throughput limits"
category: rate-limit
source: https://github.com/anthropics/claude-code/issues/25805
---

# Rate limit errors show 'API Error' without distinguishing usage vs throughput limits

## 증상
Users on paid plans (including Pro Max $200/month) are encountering `API Error: Rate limit reached` messages when their usage dashboard shows they haven't hit their limits. This creates confusion and disrupts workflow because the error message doesn't clarify **what** limit was reached.

## 원인
보고된 버그/문제. 카테고리: rate-limit.

## 해결법
(wait 60s vs stop for the week)

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/25805
