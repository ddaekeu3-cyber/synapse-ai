---
layout: solution
title: "[FEATURE] Bring 1M context window to Max plan subscribers"
category: context-window
source: https://github.com/anthropics/claude-code/issues/23879
description: "- [x] I have searched existing requests and this feature hasn't been requested"
---

# [FEATURE] Bring 1M context window to Max plan subscribers

## 증상
- [x] I have searched [existing requests](https://github.com/anthropics/claude-code/issues?q=is%3Aissue%20label%3Aenhancement) and this feature hasn't been requested yet

## 원인
Input exceeded the model's maximum context length, causing truncation or a refusal to process the full request. 카테고리: context-window.

## 해결법
isn't one. Telling Max subscribers to switch to pay-as-you-go API access to get 1M means paying more on top of an already premium subscription — and losing the convenience of the integrated VSCode experience with session history.

Suggestion: Offer the 1M context window to Max subscribers, even if it means reduced rate limits or a usage cap when using the extended context. A 1M window with fewer messages per day would be far more valuable than unlimited 200k conversations that keep getting compacted.

I'd love to hear if there's a roadmap for bringing this feature to Max plans. As it stands, t

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/23879
