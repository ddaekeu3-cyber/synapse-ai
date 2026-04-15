---
layout: solution
title: "Behavior bug"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/50748
description: "Behavior bug (incorrect output/state without"
---

# Behavior bug

## 증상
Behavior bug (incorrect output/state without crash)

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
### Additional information

Workaround: Set agents.defaults.model.primary: "minimax/MiniMax-M2.7" directly in config instead of using /model command. The bug is in model-selection-*.js around line 88343 where buildAllowedModelSet and buildModelAliasIndex are called with the wrong defaultProvider ("anthropic" instead of the session's configured provider).

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/50748
