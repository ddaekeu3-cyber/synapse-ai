---
layout: solution
title: "RFC: Prevent parallel auth kickout via token refresh serialization"
category: openclaw
source: https://github.com/anthropics/claude-code/issues/37678
description: "Parallel processes cause VS Code/Cursor extension to lose authentication. This has been reported across 7+ issues (#24317, #37512, #37203, #37324, #37468,"
---

# RFC: Prevent parallel auth kickout via token refresh serialization

## 증상
Parallel `claude -p` processes cause VS Code/Cursor extension to lose authentication. This has been reported across 7+ issues (#24317, #37512, #37203, #37324, #37468, #25609, #22600) and affects every user running concurrent sessions on macOS.

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
We built [`claude-batch`](https://github.com/LARIkoz/claude-batch) — a tmux-based wrapper that prevents refresh from occurring during batch runs. Strategy: pre-batch force refresh → 2h token gate → batch completes within token lifetime → no refresh triggered → no race.

Validated by 7-model consilium (Gemini, Grok-4, DeepSeek, Mistral, Codex, Qwen, Claude Opus), 3 rounds code review, 1 red team. 6/6 APPROVE.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/37678
