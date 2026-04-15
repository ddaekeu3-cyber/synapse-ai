---
layout: solution
title: "Scheduled tasks: 'Always allow' option missing from permission prompts"
category: general
source: https://github.com/anthropics/claude-code/issues/33027
description: "When a scheduled task triggers a permission prompt during execution, only the \"Allow once\" option is presented. The \"Always allow\" option that appears in"
---

# Scheduled tasks: 'Always allow' option missing from permission prompts

## 증상
When a scheduled task triggers a permission prompt during execution, only the "Allow once" option is presented. The "Always allow" option that appears in interactive CLI sessions is missing.

## 원인
Agent encountered an unexpected state or unhandled error condition outside the standard error handling path.

## 해결법
Pre-configuring exact command patterns in `~/.claude/settings.json` under `allowedTools` works for some commands, but the interactive "Always allow" flow would be much more ergonomic for discovering which permissions a task needs.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/33027
