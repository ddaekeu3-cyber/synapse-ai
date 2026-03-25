---
layout: solution
title: "OTEL telemetry ECONNRESET propagates to main event loop, making CLI unusable"
category: performance
source: https://github.com/anthropics/claude-code/issues/37079
---

# OTEL telemetry ECONNRESET propagates to main event loop, making CLI unusable

## 증상
When the OpenTelemetry exporter encounters ECONNRESET, the error propagates to the main event loop and **makes the entire CLI unresponsive**. The process stays alive but no input is processed. This is a critical violation of the principle that observability infrastructure must never affect application availability.

## 원인
보고된 버그/문제. 카테고리: performance.

## 해결법
is ~30 lines and carries zero risk to the main API workflow.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/37079
