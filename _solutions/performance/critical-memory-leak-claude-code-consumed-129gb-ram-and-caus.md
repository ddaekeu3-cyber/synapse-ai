---
layout: solution
title: "Critical Memory Leak: Claude Code Consumed 129GB RAM and Caused System Freeze"
category: performance
source: https://github.com/anthropics/claude-code/issues/11315
---

# Critical Memory Leak: Claude Code Consumed 129GB RAM and Caused System Freeze

## 증상
Claude Code experienced a severe memory leak that consumed 129GB of virtual memory, exhausted all available system RAM (16GB), and caused a complete system freeze requiring a hard reboot.

## 원인
보고된 버그/문제. 카테고리: performance.

## 해결법
Currently monitoring with: `watch -n 1 'ps aux --sort=-%mem | head -5'`
Plan to restart Claude Code when memory usage exceeds safe threshold.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/11315
