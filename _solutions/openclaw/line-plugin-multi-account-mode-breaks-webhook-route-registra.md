---
layout: solution
title: "LINE plugin: multi-account mode breaks webhook route registration (404)"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/47264
---

# LINE plugin: multi-account mode breaks webhook route registration (404)

## 증상
When configuring LINE with multiple accounts using `channels.line.accounts`, the webhook routes fail to register — all LINE webhook endpoints return 404 (Not Found).

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Using single-account LINE config only.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/47264
