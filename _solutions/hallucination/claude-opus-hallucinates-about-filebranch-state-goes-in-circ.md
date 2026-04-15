---
layout: solution
title: "Claude Opus hallucinates about file/branch state, goes in circles"
category: hallucination
source: https://github.com/anthropics/claude-code/issues/36174
description: "During a multi-step development session, Claude Opus 4.6 (1M context) repeatedly hallucinated about the state of files and branches, wasting significant"
---

# Claude Opus hallucinates about file/branch state, goes in circles

## 증상
During a multi-step development session, Claude Opus 4.6 (1M context) repeatedly hallucinated about the state of files and branches, wasting significant time going in circles.

## 원인
Model generated plausible but incorrect output due to insufficient grounding, missing verification, or high sampling temperature.

## 해결법
was already on `origin/main` and that "no changes needed." When `git show origin/main:file` was run, it showed the old code — but Claude still insisted the fix was there. This happened multiple times in a loop.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/36174
