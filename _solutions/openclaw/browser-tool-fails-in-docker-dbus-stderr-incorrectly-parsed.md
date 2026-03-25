---
layout: solution
title: "Browser tool fails in Docker: dbus stderr incorrectly parsed as fatal launch error"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/47466
---

# Browser tool fails in Docker: dbus stderr incorrectly parsed as fatal launch error

## 증상
- OpenClaw: 2026.3.13 (Docker, Debian bookworm)

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Use PinchTab or Playwright directly instead of the built-in browser tool.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/47466
