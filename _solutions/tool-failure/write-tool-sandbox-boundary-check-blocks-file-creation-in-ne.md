---
layout: solution
title: "write tool: Sandbox boundary check blocks file creation in new directories"
category: tool-failure
source: https://github.com/openclaw/openclaw/issues/29700
---

# write tool: Sandbox boundary check blocks file creation in new directories

## 증상
The `write` tool fails with `Sandbox boundary checks failed; cannot create directories` when attempting to write a file to a directory that was created during the same session via `exec`/shell.

## 원인
보고된 버그/문제. 카테고리: tool-failure.

## 해결법
Use `exec` with a heredoc to write the file content instead of the `write` tool.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/29700
