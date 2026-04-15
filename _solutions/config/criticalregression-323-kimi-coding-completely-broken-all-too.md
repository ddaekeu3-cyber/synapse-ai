---
layout: solution
title: "[Critical][Regression 3.23] kimi-coding completely broken: all tool calls return empty {} args due to missing moonshot-thinking payload compat"
category: config
source: https://github.com/openclaw/openclaw/issues/53591
description: "Regression (worked before, now"
---

# [Critical][Regression 3.23] kimi-coding completely broken: all tool calls return empty {} args due to missing moonshot-thinking payload compat

## 증상
Regression (worked before, now fails)

## 원인
GitHub Issue #53591에서 보고된 버그. 해결법이 이슈에 포함됨.

## 해결법
Switch to `minimax/MiniMax-M2.7` (same Anthropic API, works correctly).

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/53591
