---
layout: solution
title: "OAuth login for openai-codex fails after callback URL is pasted on Windows 11 (`TypeError: fetch failed`)"
category: auth
source: https://github.com/openclaw/openclaw/issues/45036
---

# OAuth login for openai-codex fails after callback URL is pasted on Windows 11 (`TypeError: fetch failed`)

## 증상
Behavior bug (incorrect output/state without crash)

## 원인
GitHub Issue #45036에서 보고된 버그. 해결법이 이슈에 포함됨.

## 해결법
### Additional information

I do not currently know whether this is Windows-specific, proxy/VPN-related, or a more general OAuth exchange issue.

What is confirmed so far:
- the browser opens correctly
- authentication can be completed in the browser
- the localhost callback URL is produced
- the failure happens only after the callback URL is submitted back to OpenClaw

A useful next step would be guidance on:
- whether there is a debug flag for the OAuth exchange step
- which internal request `

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/45036
