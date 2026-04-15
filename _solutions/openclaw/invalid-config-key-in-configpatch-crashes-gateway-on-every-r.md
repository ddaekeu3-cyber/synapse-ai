---
layout: solution
title: "Invalid config key in config.patch crashes gateway on every restart (no validation)"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/40264
description: "Passing an unrecognized key to (or ) writes the invalid config to disk without validation. The gateway then crashes on every subsequent restart attempt,"
---

# Invalid config key in config.patch crashes gateway on every restart (no validation)

## 증상
Passing an unrecognized key to `config.patch` (or `config.apply`) writes the invalid config to disk without validation. The gateway then crashes on every subsequent restart attempt, requiring a manual config file edit to recover.

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
Run `config.schema` first to check valid keys before calling `config.patch`. Manually edit the config file at `~/.clawdbot/config.yaml` (or equivalent) to remove the bad key.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/40264
