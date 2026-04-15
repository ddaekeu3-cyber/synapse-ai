---
layout: solution
title: "Feature Request: Allow project-scoped memory to be stored inside the repository for cross-device sync"
category: general
source: https://github.com/anthropics/claude-code/issues/38519
description: "Claude Code's memory system () is stored in the user's home directory, making it local to a single machine. When users work across multiple devices, all"
---

# Feature Request: Allow project-scoped memory to be stored inside the repository for cross-device sync

## 증상
Claude Code's memory system (`~/.claude/projects/.../memory/`) is stored in the user's home directory, making it **local to a single machine**. When users work across multiple devices, all accumulated memory — feedback corrections, workflow preferences, learned patterns — is lost on each new machine.

## 원인
Agent encountered an unexpected state or unhandled error condition outside the standard error handling path.

## 해결법
degrades both use cases.**

Forcing everything into `CLAUDE.md` means:
- Project-level `CLAUDE.md` gets bloated with personal preferences that other contributors don't need
- Personal feedback like "don't mock databases in tests — we got burned last quarter" sits next to architectural docs
- Users must manually migrate rules between memory and `CLAUDE.md`, which is error-prone

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/38519
