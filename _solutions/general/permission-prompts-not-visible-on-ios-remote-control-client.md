---
layout: solution
title: "Permission prompts not visible on iOS remote control client"
category: general
source: https://github.com/anthropics/claude-code/issues/28427
---

# Permission prompts not visible on iOS remote control client

## 증상
When using Remote Control to mirror a local Claude Code session to the iOS Claude app, permission prompts and mode controls behave inconsistently on the mobile client.

## 원인
보고된 버그/문제. 카테고리: general.

## 해결법
is to set bypass mode on desktop before walking away, but this removes all safety gates for the duration of the mobile session.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/28427
