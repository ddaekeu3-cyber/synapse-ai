---
layout: solution
title: "Dashboard model picker sends anthropic/ prefix for non-Anthropic models"
category: config
source: https://github.com/openclaw/openclaw/issues/51809
---

# Dashboard model picker sends anthropic/ prefix for non-Anthropic models

## 증상
Regression (worked before, now fails)

## 원인
GitHub Issue #51809에서 보고된 버그. 해결법이 이슈에 포함됨.

## 해결법
regardless of the selected model's actual provider

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/51809
