---
layout: solution
title: "skill-creator: improve_description.py fails with thinking.type 'adaptive' on current SDK"
category: openclaw
source: https://github.com/anthropics/claude-code/issues/33466
---

# skill-creator: improve_description.py fails with thinking.type 'adaptive' on current SDK

## 증상
The `skill-creator` plugin's `improve_description.py` script uses `thinking={"type": "adaptive", "budget_tokens": 10000}` in its Anthropic API calls (lines 117-119 and 154-156). This fails with the current public Anthropic Python SDK (v0.84.0):

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Change `"type": "adaptive"` to `"type": "enabled"` in both occurrences in `improve_description.py`. This uses full extended thinking instead of adaptive, which works on the current SDK.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/33466
