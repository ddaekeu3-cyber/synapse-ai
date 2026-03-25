---
layout: solution
title: "Feature Request: Built-in Agent Cost & Usage Analytics"
category: token-cost
source: https://github.com/openclaw/openclaw/issues/39297
---

# Feature Request: Built-in Agent Cost & Usage Analytics

## 증상
There is no native way to answer basic operational questions:

## 원인
보고된 버그/문제. 카테고리: token-cost.

## 해결법
I built `cost-report.sh` that parses custom gatekeeper logs and estimates savings. It works but requires maintaining a parallel logging system and hardcoding model pricing. This should be a platform feature.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/39297
