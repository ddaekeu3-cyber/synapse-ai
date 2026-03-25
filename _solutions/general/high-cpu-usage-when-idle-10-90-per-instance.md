---
layout: solution
title: "High CPU usage when idle (10-90% per instance)"
category: general
source: https://github.com/anthropics/claude-code/issues/30807
---

# High CPU usage when idle (10-90% per instance)

## 증상
Claude Code uses significant CPU (10-90% per instance) even when completely idle and waiting for user input. This becomes problematic when running multiple instances.

## 원인
보고된 버그/문제. 카테고리: general.

## 해결법
Currently none - killing idle instances is the only mitigation.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/30807
