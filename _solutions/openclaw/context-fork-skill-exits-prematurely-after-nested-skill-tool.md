---
layout: solution
title: "context: fork skill exits prematurely after nested Skill tool invocation"
category: openclaw
source: https://github.com/anthropics/claude-code/issues/30256
---

# context: fork skill exits prematurely after nested Skill tool invocation

## 증상
When a skill running in `context: fork` mode invokes another skill via the `Skill` tool, the parent fork exits prematurely after the nested skill completes, instead of continuing execution.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Replace the `Skill` invocation with inline instructions that use `Write` directly to achieve the same effect, avoiding the nested `Skill` call entirely.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/30256
