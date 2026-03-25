---
layout: solution
title: "Sandbox network prompt missing 'No' option — causes infinite loop with retrying subprocesses"
category: loop-stuck
source: https://github.com/anthropics/claude-code/issues/38152
---

# Sandbox network prompt missing "No" option — causes infinite loop with retrying subprocesses

## 증상
- [x] I have searched [existing issues](https://github.com/anthropics/claude-code/issues?q=is%3Aissue%20state%3Aopen%20label%3Abug) and this hasn't been reported yet

## 원인
보고된 버그/문제. 카테고리: loop-stuck.

## 해결법
Kill the subprocess externally, or disable the tool's telemetry before enabling sandbox. In this case: `dolt config --global --add metrics.disabled true` and restart the Dolt server.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/38152
