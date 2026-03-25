---
layout: solution
title: "Interactive TUI crashes: screen width spirals unboundedly (100% write ratio)"
category: auth
source: https://github.com/anthropics/claude-code/issues/29695
---

# Interactive TUI crashes: screen width spirals unboundedly (100% write ratio)

## 증상
- **Claude Code version**: 2.1.63 (Homebrew cask, macOS)

## 원인
보고된 버그/문제. 카테고리: auth.

## 해결법
Use print/pipe mode (`claude -p "prompt"`) or non-interactive mode. Interactive TUI is currently unstable.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/29695
