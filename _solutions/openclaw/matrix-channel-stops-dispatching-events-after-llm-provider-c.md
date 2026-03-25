---
layout: solution
title: "Matrix channel stops dispatching events after LLM provider config hot-reload"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/54069
---

# Matrix channel stops dispatching events after LLM provider config hot-reload

## 증상
Regression (worked before, now fails)

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
None found. Rolling back to a previous version is not possible with the `:latest` tag.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/54069
