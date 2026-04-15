---
layout: solution
title: "Inject session/channel context env vars into agent shell environments"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/53222
description: "When an OpenClaw agent spawns a tmux session (or any child process), there is no way to automatically know which OC session, channel, or agent originated"
---

# Inject session/channel context env vars into agent shell environments

## 증상
When an OpenClaw agent spawns a tmux session (or any child process), there is no way to automatically know which OC session, channel, or agent originated the work. The shell environment has no context beyond `OPENCLAW_GATEWAY_PORT` and similar service-level vars.

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
We built a wrapper script (`tmux-new.sh`) that requires the caller to explicitly pass `CREATOR_CHANNEL` and `CREATOR_LABEL` args, plus a `session-created` hook that alerts on untagged sessions. But this relies entirely on agent discipline — there's no way to auto-detect the origin session.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/53222
