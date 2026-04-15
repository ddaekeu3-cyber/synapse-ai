---
layout: solution
title: "MCP server stays unavailable forever for existing sessions after transient startup failure"
category: hallucination
source: https://github.com/openclaw/openclaw/issues/53864
description: "Behavior bug (incorrect output/state without"
---

# MCP server stays unavailable forever for existing sessions after transient startup failure

## 증상
Behavior bug (incorrect output/state without crash)

## 원인
Model generated plausible but incorrect output due to insufficient grounding, missing verification, or high sampling temperature.

## 해결법
sees and executes
   the MCP tools correctly.

6. Return to the session from step 4. The MCP tools are still missing.
   They never appear, regardless of:
   - gateway restart (`openclaw gateway stop` / start, or `docker restart`)
   - full container reboot
   - waiting any amount of time

7. (Variant) Add a *new* MCP server to openclaw.json while sessions already exist.
   Existing sessions never see the new server's tools either.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/53864
