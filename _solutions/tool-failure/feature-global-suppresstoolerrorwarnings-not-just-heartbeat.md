---
layout: solution
title: "Feature: global suppressToolErrorWarnings (not just heartbeat)"
category: tool-failure
source: https://github.com/openclaw/openclaw/issues/46965
---

# Feature: global suppressToolErrorWarnings (not just heartbeat)

## 증상
When an agent's tool call fails (e.g. `edit` with non-matching text, sandbox path escape, or invalid config schema path), the Gateway emits a fallback tool error reply to the user. This is correct behavior for cases where the user needs to know — but creates noise for:

## 원인
보고된 버그/문제. 카테고리: tool-failure.

## 해결법
should allow suppressing this fallback for non-critical error classes.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/46965
