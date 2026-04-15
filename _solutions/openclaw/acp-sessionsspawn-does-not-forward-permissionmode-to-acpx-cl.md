---
layout: solution
title: "ACP sessions_spawn does not forward permissionMode to acpx CLI"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/51640
description: "When using with and , Claude Code's tool calls (even simple ) get auto-rejected because the acpx CLI is not receiving the"
---

# ACP sessions_spawn does not forward permissionMode to acpx CLI

## 증상
When using `sessions_spawn` with `runtime: "acp"` and `agentId: "claude"`, Claude Code's tool calls (even simple `ls`) get auto-rejected because the acpx CLI is not receiving the `--approve-all` flag.

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
Use direct acpx CLI with `--approve-all` flag instead of `sessions_spawn`.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/51640
