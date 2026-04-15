---
layout: solution
title: "--print mode silent hang on Windows — recurring across v2.1.51, v2.1.78, v2.1.81"
category: performance
source: https://github.com/anthropics/claude-code/issues/38516
description: "- [x] I have searched existing issues and this hasn't been reported yet (similar: #37660, #37154, #33949 — but this documents cross-version"
---

# --print mode silent hang on Windows — recurring across v2.1.51, v2.1.78, v2.1.81

## 증상
- [x] I have searched existing issues and this hasn't been reported yet (similar: #37660, #37154, #33949 — but this documents cross-version recurrence)

## 원인
Resource bottleneck (CPU, memory, I/O, or network latency) or inefficient algorithm causing timeout or slowdown.

## 해결법
None for `--print` mode. We switched to manual file editing via a separate tool pipeline. Interactive mode (with PTY) was not tested this session but has been unreliable on Windows in the past.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/38516
