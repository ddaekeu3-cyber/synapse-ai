---
layout: solution
title: "Notification hook receives phantom `permission_prompt` events with no visible permission dialog"
category: general
source: https://github.com/anthropics/claude-code/issues/16102
---

# Notification hook receives phantom `permission_prompt` events with no visible permission dialog

## 증상
- [x] I have searched [existing issues](https://github.com/anthropics/claude-code/issues?q=is%3Aissue%20state%3Aopen%20label%3Abug) and this hasn't been reported yet

## 원인
보고된 버그/문제. 카테고리: general.

## 해결법
A marker-based suppression system could filter phantom `permission_prompt` events that follow `AskUserQuestion` within a short window (similar to the workaround for the Stop hook firing multiple times during git workflows - see issue #15250).

However, this workaround should not be necessary - `permission_prompt` events should only fire when a permission dialog is actually displayed.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/16102
