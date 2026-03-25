---
layout: solution
title: "Auto-backup Task outputs before context compaction"
category: token-cost
source: https://github.com/anthropics/claude-code/issues/31420
---

# Auto-backup Task outputs before context compaction

## 증상
- [x] I have searched [existing requests](https://github.com/anthropics/claude-code/issues?q=is%3Aissue%20label%3Aenhancement) and this feature hasn't been requested yet

## 원인
보고된 버그/문제. 카테고리: token-cost.

## 해결법
Users must manually save each Task output immediately after TaskOutput():

  // Launch agent
  const taskId = await Task("analyze codebase from tester perspective");

  // IMMEDIATELY save result (before context compact)
  const output = await TaskOutput(taskId);
  await Write("results/agent-1-tester.md", output);

  // Repeat for EVERY agent (7+ agents in a SWARM)

  Problems with manual approach:
  - ❌ Easy to forget when managing 7+ agents
  - ❌ Requires strict discipline and interrupts flow
  - ❌ Still fails if compaction happens mid-workflow
  - ❌ No way to recover if forgotten

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/31420
