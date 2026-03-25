---
layout: solution
title: "Slack: Thread sessions don't bootstrap with thread context"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/44638
---

# Slack: Thread sessions don't bootstrap with thread context

## 증상
When a user replies in a Slack thread, OpenClaw creates a new nested session for that thread. However, this session starts blank with no context of the parent message or previous thread replies, causing the agent to respond without understanding the conversation.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Agent can manually call Slack API via curl if it detects it's in a thread session:
```bash
curl "https://slack.com/api/conversations.replies?channel=CHANNEL&ts=THREAD_TS" \
  -H "Authorization: Bearer $BOT_TOKEN"
```

But this requires the agent to know to do this proactively.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/44638
