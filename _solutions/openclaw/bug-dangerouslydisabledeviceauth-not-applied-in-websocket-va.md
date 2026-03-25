---
layout: solution
title: "Bug: dangerouslyDisableDeviceAuth not applied in WebSocket validation (Control UI)"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/41539
---

# Bug: dangerouslyDisableDeviceAuth not applied in WebSocket validation (Control UI)

## 증상
**Version:** OpenClaw 2026.3.8 (3caab92)

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
None found. Only localhost (127.0.0.1) connections work, but those fail for other reasons (cert issues).

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/41539
