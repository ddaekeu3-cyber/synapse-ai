---
layout: solution
title: "Control UI 'device signature invalid' — token field mismatch between client signing and server"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/39667
description: "Regression (worked before, now"
---

# Control UI "device signature invalid" — token field mismatch between client signing and server

## 증상
Regression (worked before, now fails)

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
A Tampermonkey userscript that injects `#token=<shared-token>` into the URL on every page load (`@run-at document-start`), before the app's `<script type="module">` executes. The app reads the token from the URL hash in `applySettingsFromUrl()`, re-establishing the connection on each refresh.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/39667
