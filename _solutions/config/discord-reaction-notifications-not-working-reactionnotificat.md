---
layout: solution
title: "Discord reaction notifications not working (reactionNotifications: own)"
category: config
source: https://github.com/openclaw/openclaw/issues/26956
---

# Discord reaction notifications not working (reactionNotifications: own)

## 증상
Discord reaction notifications are not being received by the agent, despite configuration being set correctly.

## 원인
보고된 버그/문제. 카테고리: config.

## 해결법
Using reply-based approvals ("go"/"skip") instead of reactions for now.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/26956
