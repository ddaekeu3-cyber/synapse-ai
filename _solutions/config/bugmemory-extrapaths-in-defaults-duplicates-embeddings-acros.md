---
layout: solution
title: "bug(memory): extraPaths in defaults duplicates embeddings across all agents"
category: config
source: https://github.com/openclaw/openclaw/issues/46558
description: "When is configured, every agent independently indexes and embeds all files in those paths. For a setup with N agents and M files in extraPaths, this"
---

# bug(memory): extraPaths in defaults duplicates embeddings across all agents

## 증상
When `agents.defaults.memorySearch.extraPaths` is configured, every agent independently indexes and embeds all files in those paths. For a setup with N agents and M files in extraPaths, this produces N × M embedding operations and N copies of the same vectors in separate sqlite databases.

## 원인
Environment variable, configuration file, or initialization parameter missing, malformed, or incorrectly scoped.

## 해결법
#46542 introduces a shared memory store (`sharedPaths`) that indexes once in `_shared.sqlite` and merges results into agent searches. It also soft-migrates `extraPaths` to `sharedPaths` with a deprecation warning.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/46558
