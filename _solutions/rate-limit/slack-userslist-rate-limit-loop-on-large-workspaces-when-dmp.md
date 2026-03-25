---
layout: solution
title: "Slack: users.list rate-limit loop on large workspaces when dmPolicy uses ID-based allowlist"
category: rate-limit
source: https://github.com/openclaw/openclaw/issues/31733
---

# Slack: users.list rate-limit loop on large workspaces when dmPolicy uses ID-based allowlist

## 증상
When `channels.slack.dmPolicy` is set to `allowlist` with user IDs (e.g., `allowFrom: ["U03A3QXEER3"]`), OpenClaw calls `users.list` to resolve the allowlist on startup and on incoming messages. On large Slack workspaces (thousands of users), this exhausts Slack's rate limit, causing an infinite retry loop:

## 원인
보고된 버그/문제. 카테고리: rate-limit.

## 해결법
Set `dmPolicy: disabled` to prevent the allowlist resolution from running. This disables Slack DM support entirely but stops the loop. Channel functionality (controlled by `groupPolicy`) is unaffected.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/31733
