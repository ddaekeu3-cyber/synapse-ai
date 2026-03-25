---
layout: solution
title: "Write and Edit tools fail with EEXIST error on Windows/NTFS"
category: general
source: https://github.com/anthropics/claude-code/issues/31233
---

# Write and Edit tools fail with EEXIST error on Windows/NTFS

## 증상
Starting with Claude Code 2.1.69, the `Write` and `Edit` tools intermittently fail with:

## 원인
보고된 버그/문제. 카테고리: general.

## 해결법
Use `Bash` tool with `cat > file` or a Python script to write files instead of `Write`/`Edit` tools.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/31233
