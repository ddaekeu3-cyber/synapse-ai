---
layout: solution
title: "Unknown provider 'openai-codex' via `models auth login`"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/19124
description: "$ openclaw models auth login --provider"
---

# Unknown provider "openai-codex" via `models auth login`

## 증상
$ openclaw models auth login --provider openai-codex

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
that is through `openclaw onboard`. that's it - pretty stupid that it doesn't recognize its own provider. I should be able to login with codex like I can with all others. Also the help message is misleading since codex auth isn't there.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/19124
