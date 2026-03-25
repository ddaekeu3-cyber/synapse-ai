---
layout: solution
title: "Add --max-context flag to cap context window usage"
category: context-window
source: https://github.com/anthropics/claude-code/issues/34650
---

# Add --max-context flag to cap context window usage

## 증상
With the recent upgrade of Opus 4.6 to 1M context, my API quota burns ~5x faster than before. I was working comfortably at 200K and have no need for 1M in most sessions. There's currently no way to limit the context window size Claude Code uses.

## 원인
보고된 버그/문제. 카테고리: context-window.

## 해결법
## Environment

- Claude Code on Windows (MSYS2/Git Bash)
- Opus 4.6 with 1M context
- Previously worked well with 200K limit

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/34650
