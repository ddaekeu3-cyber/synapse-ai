---
layout: solution
title: "Inject context usage percentage into system-reminder for model self-awareness"
category: config
source: https://github.com/anthropics/claude-code/issues/38526
description: "The model has no awareness of how much context it has consumed. It can't proactively save information to memory, wrap up tasks, or warn the user. The"
---

# Inject context usage percentage into system-reminder for model self-awareness

## 증상
The model has no awareness of how much context it has consumed. It can't proactively save information to memory, wrap up tasks, or warn the user. The model only discovers context pressure when compaction happens — and by then information is already lost.

## 원인
Environment variable, configuration file, or initialization parameter missing, malformed, or incorrectly scoped.

## 해결법
We've built an approximation using PostToolUse hooks that read a sidecar file written by the statusline script. This works but is fragile (depends on statusline timing, file I/O race conditions) and the output is just appended text, not a proper system-reminder that the model is trained to attend to.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/38526
