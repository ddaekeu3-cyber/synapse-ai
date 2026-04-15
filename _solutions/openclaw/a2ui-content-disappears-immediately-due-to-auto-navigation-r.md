---
layout: solution
title: "A2UI content disappears immediately due to auto-navigation reload loop"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/22292
description: "A2UI content renders momentarily then disappears because the macOS app's auto-navigation logic reloads the WebView in a loop. There are two related"
---

# A2UI content disappears immediately due to auto-navigation reload loop

## 증상
A2UI content renders momentarily then disappears because the macOS app's auto-navigation logic reloads the WebView in a loop. There are two related bugs:

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
Bug 2 (MIME type) can be patched in the compiled gateway dist file by adding extension checks before `detectMime()`. Bug 1 (auto-navigation) requires rebuilding the macOS app from source — no runtime workaround found.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/22292
