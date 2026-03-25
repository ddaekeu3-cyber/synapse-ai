---
layout: solution
title: "Telemetry opt-out should not disable Channels feature flag"
category: config
source: https://github.com/anthropics/claude-code/issues/38450
---

# Telemetry opt-out should not disable Channels feature flag

## 증상
`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` disables GrowthBook feature flag evaluation, which silently breaks Channels (`--channels ignored (Channels are not currently available)`).

## 원인
보고된 버그/문제. 카테고리: config.

## 해결법
(completely removing the env var) forces users to choose between privacy preferences and product features.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/38450
