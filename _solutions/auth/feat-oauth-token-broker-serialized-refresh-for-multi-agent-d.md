---
layout: solution
title: "feat: OAuth token broker — serialized refresh for multi-agent deployments"
category: auth
source: https://github.com/openclaw/openclaw/issues/47908
---

# feat: OAuth token broker — serialized refresh for multi-agent deployments

## 증상
When multiple agents share an OAuth provider (e.g., `openai-codex`), they all hold the same refresh token. OAuth refresh tokens are single-use — when Agent A refreshes, the old token is invalidated. If Agent B refreshes with the now-stale token before receiving the new one, both tokens die. The user must re-authenticate.

## 원인
보고된 버그/문제. 카테고리: auth.

## 해결법
We stagger agent restarts by 4 seconds so heartbeat timers don't collide. This reduces but doesn't eliminate the race.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/47908
