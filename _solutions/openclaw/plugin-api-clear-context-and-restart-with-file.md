---
layout: solution
title: "Plugin API: clear context and restart with file"
category: openclaw
source: https://github.com/anthropics/claude-code/issues/37292
---

# Plugin API: clear context and restart with file

## 증상
Building iterative dev loop plugins (like [rl](https://github.com/0xbigboss/rl)) where the workflow has two distinct phases:

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Write the plan to disk (`.rl/prompt.md`), rely on natural compaction to compress the planning conversation, and use the stop hook's `reason` field to inject the plan as a fresh directive. This works but the planning context stays in the window competing with execution context.

The cleanest alternative today is telling users to start a new `claude` session manually, which breaks the flow.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/37292
