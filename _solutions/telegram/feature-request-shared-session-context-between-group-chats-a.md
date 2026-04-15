---
layout: solution
title: "Feature Request: Shared session context between group chats and DMs"
category: telegram
source: https://github.com/openclaw/openclaw/issues/51805
description: "Currently, group chats and DMs always have isolated sessions ( vs ). There is no way to share conversation context between a group chat and a private chat"
---

# Feature Request: Shared session context between group chats and DMs

## 증상
Currently, group chats and DMs always have isolated sessions (`group:<key>` vs `direct:<key>`). There is no way to share conversation context between a group chat and a private chat with the same user.

## 원인
Telegram Bot API conflict, rate limit, or webhook/polling configuration error causing message delivery failure.

## 해결법
- Use `MEMORY.md` in the workspace for cross-session memory (file-based, not real-time context)
- Manually summarize and paste context between channels

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/51805
