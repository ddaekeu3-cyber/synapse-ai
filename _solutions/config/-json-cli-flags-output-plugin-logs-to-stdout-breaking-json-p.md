---
layout: solution
title: "`--json` CLI flags output plugin logs to stdout, breaking JSON parsing"
category: config
source: https://github.com/openclaw/openclaw/issues/52032
---

# `--json` CLI flags output plugin logs to stdout, breaking JSON parsing

## 증상
Behavior bug (incorrect output/state without crash)

## 원인
GitHub Issue #52032에서 보고된 버그. 해결법이 이슈에 포함됨.

## 해결법
should be applied globally in the preAction hook rather than per-command.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/52032
