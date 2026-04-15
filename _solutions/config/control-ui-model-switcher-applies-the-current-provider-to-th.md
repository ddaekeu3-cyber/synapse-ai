---
layout: solution
title: "Control UI model switcher applies the current provider to the selected model"
category: config
source: https://github.com/openclaw/openclaw/issues/46859
description: "Regression (worked before, now"
---

# Control UI model switcher applies the current provider to the selected model

## 증상
Regression (worked before, now fails)

## 원인
GitHub Issue #46859에서 보고된 버그. 해결법이 이슈에 포함됨.

## 해결법
- Build picker option values from the catalog's full `provider/model` identity
- Preserve the provider in the locally cached selected value
- Add a regression test to ensure the picker submits the full model reference

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/46859
