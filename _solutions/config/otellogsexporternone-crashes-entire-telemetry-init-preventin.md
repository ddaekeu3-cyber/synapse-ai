---
layout: solution
title: "OTEL_LOGS_EXPORTER=none crashes entire telemetry init, preventing metrics export"
category: config
source: https://github.com/anthropics/claude-code/issues/38454
---

# OTEL_LOGS_EXPORTER=none crashes entire telemetry init, preventing metrics export

## 증상
Setting `OTEL_LOGS_EXPORTER=none` (a standard OpenTelemetry SDK value meaning "disable this signal") causes Claude Code's entire telemetry initialization to crash, silently disabling **all** OTEL export including metrics and traces.

## 원인
보고된 버그/문제. 카테고리: config.

## 해결법
Set `OTEL_LOGS_EXPORTER=""` (empty string) instead of `"none"`.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/38454
