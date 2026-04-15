---
layout: solution
title: "Voice push-to-talk regression in v2.1.83: space bar passes through to terminal while listening, no voice detected"
category: general
source: https://github.com/anthropics/claude-code/issues/38577
description: "After updating from v2.1.81 to v2.1.83, space bar push-to-talk no longer works"
---

# Voice push-to-talk regression in v2.1.83: space bar passes through to terminal while listening, no voice detected

## 증상
After updating from v2.1.81 to v2.1.83, space bar push-to-talk no longer works correctly.

## 원인
Agent encountered an unexpected state or unhandled error condition outside the standard error handling path.

## 해결법
Rebinding push-to-talk to `meta+k` in `~/.claude/keybindings.json` avoids the conflict.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/38577
