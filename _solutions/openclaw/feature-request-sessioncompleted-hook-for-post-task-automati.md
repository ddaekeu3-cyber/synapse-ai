---
layout: solution
title: "Feature request: session:completed hook for post-task automation"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/42247
---

# Feature request: session:completed hook for post-task automation

## 증상
OpenClaw provides `agent:bootstrap` hooks (pre-session) via `bootstrap-extra-files`, `forced-recall`, and `auto-capture`. But there is no corresponding post-session hook. This means there is no infrastructure-level way to run automation after a task completes — only behavioral instructions that agents may skip.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Appending `LAST: bash tools/post-action.sh ...` to cron messages and sub-agent task descriptions. This works for ~80% of cases but is behavioral, not architectural.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/42247
