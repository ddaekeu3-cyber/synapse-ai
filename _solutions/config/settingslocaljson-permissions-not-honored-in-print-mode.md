---
layout: solution
title: "settings.local.json permissions not honored in --print mode"
category: config
source: https://github.com/anthropics/claude-code/issues/35601
description: "When running , the and lists in are not applied. All Bash commands are denied regardless of matching allow"
---

# settings.local.json permissions not honored in --print mode

## 증상
When running `claude --print --permission-mode acceptEdits`, the `permissions.allow` and `permissions.deny` lists in `.claude/settings.local.json` are not applied. All Bash commands are denied regardless of matching allow patterns.

## 원인
Environment variable, configuration file, or initialization parameter missing, malformed, or incorrectly scoped.

## 해결법
Using `--permission-mode bypassPermissions` instead of `acceptEdits` bypasses the issue but also bypasses all permission controls, including deny lists.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/35601
