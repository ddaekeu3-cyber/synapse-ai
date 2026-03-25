---
layout: solution
title: "Improve error message when skill/agent frontmatter specifies unsupported model"
category: openclaw
source: https://github.com/anthropics/claude-code/issues/36116
---

# Improve error message when skill/agent frontmatter specifies unsupported model

## 증상
When a skill or agent specifies a model in its YAML frontmatter that isn't available in the user's environment, the error message is generic and doesn't indicate the source of the model string:

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
it without investigating on their own.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/36116
