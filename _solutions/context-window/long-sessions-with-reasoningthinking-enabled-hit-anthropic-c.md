---
layout: solution
title: "Long sessions with reasoning/thinking enabled hit Anthropic context limits"
category: context-window
source: https://github.com/openclaw/openclaw/issues/24767
---

# Long sessions with reasoning/thinking enabled hit Anthropic context limits

## 증상
In channels with reasoning enabled (e.g. iMessage with thinking blocks), sessions accumulate thinking block tokens in the stored conversation history. After a sufficient number of messages (~300-400 with thinking on), Anthropic's API rejects the request with:

## 원인
보고된 버그/문제. 카테고리: context-window.

## 해결법
Manually clearing the session history resolves it temporarily. Using sub-agents for long tasks avoids accumulation in the main session.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/24767
