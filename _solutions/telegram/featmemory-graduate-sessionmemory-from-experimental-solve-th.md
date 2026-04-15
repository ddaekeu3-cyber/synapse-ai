---
layout: solution
title: "feat(memory): graduate sessionMemory from experimental — solve the multi-session continuity problem"
category: telegram
source: https://github.com/openclaw/openclaw/issues/51386
description: "When a user talks to their agent on webchat and switches to Telegram, the agent on Telegram has no knowledge of the webchat conversation. It feels like"
---

# feat(memory): graduate sessionMemory from experimental — solve the multi-session continuity problem

## 증상
When a user talks to their agent on webchat and switches to Telegram, the agent on Telegram has no knowledge of the webchat conversation. It feels like two separate people — "twin brothers who don't share memory."

## 원인
Telegram Bot API conflict, rate limit, or webhook/polling configuration error causing message delivery failure.

## 해결법
already exists: `experimental.sessionMemory`, which automatically indexes all session transcripts so `memory_search` can find context from any channel. It was added in January 2026 and has been heavily developed since. But it's still experimental, so the vast majority of users never discover it.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/51386
