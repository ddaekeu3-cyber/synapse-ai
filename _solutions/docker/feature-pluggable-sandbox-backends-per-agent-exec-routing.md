---
layout: solution
title: "Feature: Pluggable sandbox backends & per-agent exec routing"
category: docker
source: https://github.com/openclaw/openclaw/issues/12405
description: "Currently, OpenClaw sandboxing is Docker-only (), and is a global setting — all agents share the same exec target. This creates two"
---

# Feature: Pluggable sandbox backends & per-agent exec routing

## 증상
Currently, OpenClaw sandboxing is Docker-only (`sandbox.docker.image`), and `tools.exec.host` is a **global** setting — all agents share the same exec target. This creates two gaps:

## 원인
Container permission, networking, or environment variable misconfiguration inside the sandbox.

## 해결법
is manual `orb run` orchestration or telling the agent to use `host=node` in its system prompt.

Related: [Brad Fitzpatrick (crawshaw) recommends VMs over Docker](https://x.com/bradfitz) for agent sandboxing — "You have to turn off the sandbox... use a fresh VM." exe.dev is built on this premise with `exe.new/openclaw` as a documented use case.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/12405
