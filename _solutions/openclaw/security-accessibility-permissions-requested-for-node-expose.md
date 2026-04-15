---
layout: solution
title: "[Security] Accessibility permissions requested for 'node' exposes all npm packages to GUI automation"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/7227
description: "OpenClaw's Peekaboo skill (and potentially other macOS GUI automation features) triggers macOS to request Accessibility permissions for the executable"
---

# [Security] Accessibility permissions requested for 'node' exposes all npm packages to GUI automation

## 증상
OpenClaw's Peekaboo skill (and potentially other macOS GUI automation features) triggers macOS to request **Accessibility permissions for the `node` executable itself**, not for a specific OpenClaw app bundle. This is a significant security risk.

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
Users concerned about security can disable Peekaboo:

```json
{
  "skills": {
    "entries": {
      "peekaboo": { "enabled": false }
    }
  }
}
```

But this requires users to:
1. Know about the security risk
2. Know how to configure skills
3. Manually edit config

**Most users will not do this.**

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/7227
