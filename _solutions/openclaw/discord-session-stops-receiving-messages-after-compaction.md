---
layout: solution
title: "Discord session stops receiving messages after Compaction"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/41352
---

# Discord session stops receiving messages after Compaction

## 증상
After a Compaction event is triggered in a Discord session, the session stops receiving new messages. The only way to recover is to restart the gateway.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Restart the gateway when this occurs.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/41352
