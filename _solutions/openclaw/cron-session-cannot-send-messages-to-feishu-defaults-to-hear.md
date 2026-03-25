---
layout: solution
title: "Cron session cannot send messages to Feishu - defaults to 'heartbeat' target"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/49116
---

# Cron session cannot send messages to Feishu - defaults to 'heartbeat' target

## 증상
When a cron job triggers a task that uses the `message` tool to send a message to Feishu, the target defaults to `"heartbeat"` instead of the correct user/chat ID, causing the message delivery to fail.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Modify scripts to call Feishu API directly instead of using OpenClaw's `message` tool.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/49116
