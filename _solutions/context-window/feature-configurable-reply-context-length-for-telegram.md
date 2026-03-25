---
layout: solution
title: "[Feature] Configurable reply context length for Telegram"
category: context-window
source: https://github.com/openclaw/openclaw/issues/6975
---

# [Feature] Configurable reply context length for Telegram

## 증상
When using `[[reply_to_current]]` or `[[reply_to:<id>]]`, the quoted message excerpt in Telegram replies is truncated. A configurable `replyContextLength` parameter would allow users to control how much of the original message is included in replies.

## 원인
보고된 버그/문제. 카테고리: context-window.

## 해결법
None — the truncation length is not configurable.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/6975
