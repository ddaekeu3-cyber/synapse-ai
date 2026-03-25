---
layout: solution
title: "Slack: userTokenReadOnly: false has no effect on read operations — reads always prefer userToken"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/42752
---

# Slack: userTokenReadOnly: false has no effect on read operations — reads always prefer userToken

## 증상
Setting `userTokenReadOnly: false` in the Slack channel config does not cause read operations to use the bot token. Reads always prefer the user token regardless of this setting, causing `missing_scope` errors when the user token lacks `channels:history` / `groups:history`.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Remove `userToken` from the Slack config entirely so reads fall through to the bot token. Tradeoff: loses user-token-only capabilities like `search:read`.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/42752
