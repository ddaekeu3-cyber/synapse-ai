---
layout: solution
title: "Plugin skills don't support `disable-model-invocation` like user skills do"
category: openclaw
source: https://github.com/anthropics/claude-code/issues/22345
---

# Plugin skills don't support `disable-model-invocation` like user skills do

## 증상
User-defined skills support `disable-model-invocation: true` in YAML frontmatter to hide them from the model's auto-detection. Plugin-defined skills lack this capability, forcing all plugin skills into context regardless of relevance.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Currently none. Users must either:
- Accept the context overhead, or
- Disable the entire plugin

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/22345
