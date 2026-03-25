---
layout: solution
title: "fork_session drops parallel tool call branches from JSONL history"
category: tool-failure
source: https://github.com/anthropics/claude-code/issues/37779
---

# fork_session drops parallel tool call branches from JSONL history

## 증상
When using `fork_session=True` with `continue_conversation=True` via the Claude Agent SDK, the CLI's fork logic drops parallel tool call branches from the previous session's JSONL history. The fork walks the `parentUuid` chain from leaf to root, producing a linear chain — but parallel tool calls create a **tree** structure in the JSONL, so sibling branches (tool_use + tool_result pairs not on the 

## 원인
보고된 버그/문제. 카테고리: tool-failure.

## 해결법
Setting `fork_session=False` while keeping `continue_conversation=True` avoids the issue — the CLI appends to the existing JSONL instead of forking, preserving the complete tree. The tradeoff is losing per-turn session ID tracking.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/37779
