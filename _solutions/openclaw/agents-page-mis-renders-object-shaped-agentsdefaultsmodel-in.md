---
layout: solution
title: "Agents page mis-renders object-shaped agents.defaults.model in Control UI"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/54252
---

# Agents page mis-renders object-shaped agents.defaults.model in Control UI

## 증상
Regression (worked before, now fails)

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
updating the container image, and the issue still reproduces after:

updating runtime/UI to 2026.3.23
confirming new UI assets are being served
hard refreshing the frontend

Suspected cause
The current frontend appears to have separate display and form-hydration paths for model config:

display path correctly handles object-shaped model config (primary / fallbacks)
editor hydration appears to normalize differently and falls back to another configured model option

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/54252
