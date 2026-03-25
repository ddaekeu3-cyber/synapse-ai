---
layout: solution
title: "o3/reasoning models fail with Codex OAuth due to auto-injected reasoning.summary requiring org verification"
category: auth
source: https://github.com/openclaw/openclaw/issues/34651
---

# o3/reasoning models fail with Codex OAuth due to auto-injected reasoning.summary requiring org verification

## 증상
Using OpenClaw 2026.3.2 with Codex OAuth (ChatGPT Pro subscription), attempting to use o3 or gpt-5.2 with `reasoning.effort` fails with:

## 원인
보고된 버그/문제. 카테고리: auth.

## 해결법
Setting `params: { reasoning: { effort: "medium" }, include: [] }` did not work - the summary parameter still appears to be injected by OpenClaw internally, not from user config.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/34651
