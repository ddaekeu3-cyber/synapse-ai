---
layout: solution
title: "Session appears stuck when bash command disrupts network connectivity"
category: loop-stuck
source: https://github.com/anthropics/claude-code/issues/34444
---

# Session appears stuck when bash command disrupts network connectivity

## 증상
- [x] I have searched [existing issues](https://github.com/anthropics/claude-code/issues?q=is%3Aissue%20state%3Aopen) for similar behavior reports

## 원인
보고된 버그/문제. 카테고리: auth.

## 해결법
is for the model to warn the user before running connectivity-disrupting commands, but ideally the tool infrastructure would detect the lost connection and report it gracefully.

Related issues: #33948, #29351

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/34444
