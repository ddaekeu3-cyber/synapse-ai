---
layout: solution
title: "Browser tool start/open/navigate actions fail with 'No supported browser found' even when Chrome is running and browser control service work"
category: gog
---

# Browser tool start/open/navigate actions fail with "No supported browser found" even when Chrome is running and browser control service work

## 증상
Regression (worked before, now fails)

에러 메시지:
```json
{
  "browser": {
    "enabled": true,
    "executablePath": "/opt/google/chrome/google-chrome",
    "headless": true,
    "noSandbox": true,
    "defaultProfile": "openclaw"
  }
}
```

### Ste

## 원인
원본 이슈에서 확인 필요. GitHub Issue #53004 참조.

## 해결법
target `/opt/google/chrome/google-chrome` works for the first launch
- The browser control service (port 18791) correctly reads config and launches Chrome, but the tool dispatch layer loses track of the running Chrome instance between calls
- `browser status` never reflects config values (`headless`, `noSandbox`, `executablePath` all show as defaults) — suggests the tool status endpoint reads from

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: gog
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/53004
