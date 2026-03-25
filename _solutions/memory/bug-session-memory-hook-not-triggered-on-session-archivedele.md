---
layout: solution
title: "Bug: session-memory hook not triggered on session archive/delete"
category: memory
source: https://github.com/openclaw/openclaw/issues/37027
---

# Bug: session-memory hook not triggered on session archive/delete

## 증상
session-memory hook only triggers on "/new" or "/reset" commands, but not when sessions are automatically archived or deleted. This causes cross-session memory loss for non-TUI channels.

## 원인
보고된 버그/문제. 카테고리: memory.

## 해결법
Manually extract key information from archived session files to memory/YYYY-MM-DD.md

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/37027
