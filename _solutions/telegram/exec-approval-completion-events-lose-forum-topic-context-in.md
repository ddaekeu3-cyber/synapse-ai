---
layout: solution
title: "Exec approval completion events lose forum topic context in Telegram forum supergroups"
category: telegram
source: https://github.com/openclaw/openclaw/issues/53659
---

# Exec approval completion events lose forum topic context in Telegram forum supergroups

## 증상
When exec approvals are configured for Telegram with `target: "dm"`, the approval prompt itself routes correctly to the user's DM with the bot. However, the **completion event** (the command result/output) loses the originating forum topic context and routes to General (topic 1) instead of the topic where the original conversation happened.

## 원인
보고된 버그/문제. 카테고리: telegram.

## 해결법
None currently. Approving from the Control UI (`http://localhost:18789`) and checking results manually.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/53659
