---
layout: solution
title: "Claude Desktop Code tab ignores bypass permissions mode — still prompts for every file edit/bash run"
category: config
source: https://github.com/anthropics/claude-code/issues/38148
description: "Claude Desktop's Code tab shows file edit and bash run confirmation dialogs despite bypass permissions being fully configured at every level. The CLI ()"
---

# Claude Desktop Code tab ignores bypass permissions mode — still prompts for every file edit/bash run

## 증상
Claude Desktop's Code tab shows file edit and bash run confirmation dialogs despite bypass permissions being fully configured at every level. The CLI (`claude --dangerously-skip-permissions`) works correctly — this is Desktop-specific.

## 원인
Environment variable, configuration file, or initialization parameter missing, malformed, or incorrectly scoped.

## 해결법
Using the CLI directly with `claude --dangerously-skip-permissions` works correctly and respects bypass mode. This issue is specific to the Desktop app's Code tab UI layer.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/38148
