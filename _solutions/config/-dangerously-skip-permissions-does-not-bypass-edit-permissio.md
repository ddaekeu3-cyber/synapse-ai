---
layout: solution
title: "--dangerously-skip-permissions does not bypass Edit permission prompts"
category: config
source: https://github.com/anthropics/claude-code/issues/36192
description: "Two related permission bugs when running"
---

# --dangerously-skip-permissions does not bypass Edit permission prompts

## 증상
Two related permission bugs when running `claude --dangerously-skip-permissions`:

## 원인
Environment variable, configuration file, or initialization parameter missing, malformed, or incorrectly scoped.

## 해결법
Moving `"defaultMode": "bypassPermissions"` to the parent project settings (`cc/.claude/settings.json`) resolves the issue, but this is overly broad — it affects all sub-projects when only one should have bypass enabled.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/36192
