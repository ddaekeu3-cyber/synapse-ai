---
layout: solution
title: "web_search/web_fetch tools not available to agent with built-in Gemini provider"
category: docker
source: https://github.com/openclaw/openclaw/issues/52677
---

# web_search/web_fetch tools not available to agent with built-in Gemini provider

## 증상
Regression (worked before, now fails)

## 원인
GitHub Issue #52677에서 보고된 버그. 해결법이 이슈에 포함됨.

## 해결법
is plugin-specific and doesn't cover built-in providers like Gemini
                                                                                                                                                                                                                                                 
  The isToolAllowedByPolicies code path identified in #51937 may also be filtering out web tools for built-in providers, but through a different registration path than plugins.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/52677
