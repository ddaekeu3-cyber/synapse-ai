---
layout: solution
title: "Feature Request: Persistent memory API for cross-session state"
category: memory
source: https://github.com/anthropics/claude-code/issues/34192
description: "Claude Code sessions are stateless by default. There is no built-in mechanism for the model to persist and retrieve structured state across sessions."
---

# Feature Request: Persistent memory API for cross-session state

## 증상
Claude Code sessions are stateless by default. There is no built-in mechanism for the model to persist and retrieve structured state across sessions. Current workarounds rely on manually managed files (CLAUDE.md, custom memory files) which the model reads on startup but cannot programmatically manage through a dedicated API.

## 원인
Agent session state was not persisted to durable storage, causing context to be lost on restart or session switch.

## 해결법
Our implementation uses:
- `CLAUDE.md` for core identity/episodic memory (~2700 lines, loaded every session)
- `MEMORY.md` for operational state
- ChromaDB + custom Python scripts for semantic search across 300K+ stored episodes
- Custom hooks (auto_recall) for automatic memory injection

This works but required months of custom development. A built-in API would make persistent memory accessible to any Claude Code user.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/34192
