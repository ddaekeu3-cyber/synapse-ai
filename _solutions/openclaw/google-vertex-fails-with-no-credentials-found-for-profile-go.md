---
layout: solution
title: "google-vertex fails with 'No credentials found for profile google-vertex:default' even when ADC is valid"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/49885
---

# google-vertex fails with "No credentials found for profile google-vertex:default" even when ADC is valid

## 증상
**Regression** — worked previously, now fails

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
* Manually normalize/fix stored auth profile
* Restart gateway

---

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/49885
