---
layout: solution
title: "QMD backend should respect agent-level memorySearch.extraPaths"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/46571
---

# QMD backend should respect agent-level memorySearch.extraPaths

## 증상
When using `memory.backend: "qmd"`, the per-agent `memorySearch.extraPaths` config is ignored. QMD builds its collections only from the agent's workspace `MEMORY.md` + `memory/**/*.md` and global `memory.qmd.paths`, but does not include paths specified in `agents.list[].memorySearch.extraPaths`.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Currently we hard-copy the reference files into the agent's workspace `memory/` directory. QMD then indexes them as part of the default `memory-dir` collection. This works but creates file redundancy.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/46571
