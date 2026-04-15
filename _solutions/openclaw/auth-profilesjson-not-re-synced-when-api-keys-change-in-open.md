---
layout: solution
title: "auth-profiles.json not re-synced when API keys change in openclaw.json"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/47808
description: "When provider API keys (e.g. ) are updated in , the gateway detects the config change and hot-reloads, but retains the old key. The cached auth profile is"
---

# auth-profiles.json not re-synced when API keys change in openclaw.json

## 증상
When provider API keys (e.g. `OPENAI_API_KEY`) are updated in `openclaw.json`, the gateway detects the config change and hot-reloads, but `~/.openclaw/agents/<id>/agent/auth-profiles.json` retains the old key. The cached auth profile is populated at onboard/setup time and never refreshed from the config.

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
Manually delete `~/.openclaw/agents/main/agent/auth-profiles.json` and restart the gateway.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/47808
