---
layout: solution
title: "User-level skills not loaded when ~/.claude/skills is a symlink (regression since ~v2.1.69)"
category: openclaw
source: https://github.com/anthropics/claude-code/issues/38051
description: "User-level skills defined in are no longer discovered/loaded when that directory is a symlink. This was working in earlier versions and appears to be a"
---

# User-level skills not loaded when ~/.claude/skills is a symlink (regression since ~v2.1.69)

## 증상
User-level skills defined in `~/.claude/skills/` are no longer discovered/loaded when that directory is a symlink. This was working in earlier versions and appears to be a regression likely introduced around v2.1.69 (which included symlink-related security fixes).

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
Replacing the symlink with a real directory and symlinking individual skill subdirectories inside it may work, but defeats the purpose of symlinking the entire skills directory for dotfiles management.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/38051
