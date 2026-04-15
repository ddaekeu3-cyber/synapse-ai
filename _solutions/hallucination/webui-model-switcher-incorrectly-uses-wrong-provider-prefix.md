---
layout: solution
title: "WebUI model switcher incorrectly uses wrong provider prefix when switching models"
category: hallucination
source: https://github.com/openclaw/openclaw/issues/54096
description: "Behavior bug (incorrect output/state without"
---

# WebUI model switcher incorrectly uses wrong provider prefix when switching models

## 증상
Behavior bug (incorrect output/state without crash)

## 원인
Model generated plausible but incorrect output due to insufficient grounding, missing verification, or high sampling temperature.

## 해결법
Manually edit `openclaw.json` configuration file:
```json
"agents": {
  "defaults": {
    "model": {
      "primary": "gemini/gemini-2.5-flash"
    }
  }
}

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/54096
