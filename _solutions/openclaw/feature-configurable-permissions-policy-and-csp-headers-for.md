---
layout: solution
title: "Feature: Configurable Permissions-Policy and CSP headers for Control UI"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/47565
description: "The gateway hardcodes security headers for the Control UI static file"
---

# Feature: Configurable Permissions-Policy and CSP headers for Control UI

## 증상
The gateway hardcodes security headers for the Control UI static file serving:

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
Manually patching the dist files:
- `microphone=()` → `microphone=(self)`
- Adding local server URLs to `connect-src`

These patches are lost on every `npm update`.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/47565
