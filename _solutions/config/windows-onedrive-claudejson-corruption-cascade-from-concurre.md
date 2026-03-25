---
layout: solution
title: "Windows + OneDrive: .claude.json corruption cascade from concurrent writes"
category: config
source: https://github.com/anthropics/claude-code/issues/29153
---

# Windows + OneDrive: .claude.json corruption cascade from concurrent writes

## 증상
On Windows 11 with the home directory on OneDrive, `~/.claude.json` experienced a cascading corruption event that resulted in complete config loss and a login loop.

## 원인
보고된 버그/문제. 카테고리: config.

## 해결법
Manual recovery by finding the largest valid `.corrupted` file in `~/.claude/backups/` and copying it back as `~/.claude.json`. The backup files are valid JSON despite the `.corrupted` naming — Claude Code was correctly saving snapshots before each recovery attempt, it just couldn't stop the cascade.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/29153
