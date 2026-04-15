---
layout: solution
title: "Bedrock provider fails when api.anthropic.com is unreachable (geo-restriction)"
category: auth
source: https://github.com/openclaw/openclaw/issues/30672
description: "When using AWS Bedrock as the model provider, OpenClaw's embedded agent still makes a direct HTTP request to . If this endpoint is unreachable (e.g. due"
---

# Bedrock provider fails when api.anthropic.com is unreachable (geo-restriction)

## 증상
When using AWS Bedrock as the model provider, OpenClaw's embedded agent still makes a direct HTTP request to `https://api.anthropic.com/api/oauth/usage`. If this endpoint is unreachable (e.g. due to Anthropic's geographic restrictions), the entire agent run fails — even though the Bedrock model inference itself would work fine.

## 원인
Authentication credential mismatch, expiry, or permission scope gap between the requesting agent and the target API.

## 해결법
Adding `https_proxy` to `~/.openclaw/.env` to proxy requests to `api.anthropic.com`.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/30672
