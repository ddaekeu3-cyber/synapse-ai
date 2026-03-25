---
layout: solution
title: "Agent hangs after user message - partial output not resumed"
category: tool-failure
source: https://github.com/openclaw/openclaw/issues/50341
---

# Agent hangs after user message - partial output not resumed

## 증상
When a user sends a message that requires the agent to perform tool calls (e.g., write to a file), sometimes the model generates a partial response and stops prematurely. The session then appears "stuck" - no further output is generated, and the agent won't respond until the user sends another message to "wake it up".

## 원인
보고된 버그/문제. 카테고리: tool-failure.

## 해결법
is for the user to send another message, but this is not ideal for automation workflows.

Tagging for visibility: @openclaw/team

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/50341
