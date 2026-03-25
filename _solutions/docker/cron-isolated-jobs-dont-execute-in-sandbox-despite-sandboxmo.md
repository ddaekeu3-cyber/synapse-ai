---
layout: solution
title: "Cron isolated jobs don't execute in sandbox despite `sandbox.mode='all'`"
category: docker
source: https://github.com/openclaw/openclaw/issues/38663
---

# Cron isolated jobs don't execute in sandbox despite `sandbox.mode="all"`

## 증상
Regression (worked before, now fails)

## 원인
보고된 버그/문제. 카테고리: docker.

## 해결법
Install required dependencies in the Gateway container instead of relying on sandbox isolation.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/38663
