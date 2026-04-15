---
layout: solution
title: "Feature: Per-agent session history visibility (granular tools.sessions.visibility)"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/19564
description: "is currently all-or-nothing. Setting it to lets every agent read every other agent's session history. Leaving it at the default blocks all cross-agent"
---

# Feature: Per-agent session history visibility (granular tools.sessions.visibility)

## 증상
`tools.sessions.visibility` is currently all-or-nothing. Setting it to `all` lets every agent read every other agent's session history. Leaving it at the default blocks all cross-agent access.

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
The coordinator can only interact with subagents via `steer` messages and wait for completion announcements. No way to check what step a subagent is on, whether it's stuck, or review its work-in-progress.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/19564
