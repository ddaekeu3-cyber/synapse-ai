---
layout: solution
title: "Anthropic setup-token silently truncated when terminal line-wraps during paste"
category: gog
---

# Anthropic setup-token silently truncated when terminal line-wraps during paste

## 증상
Regression (worked before, now fails)

에러 메시지:
```shell
Gateway error log showing auth failures:


[agent/embedded] embedded run agent end: isError=true model=claude-opus-4-6 provider=anthropic error=HTTP 401 authentication_error: Invalid bearer t

## 원인
원본 이슈에서 확인 필요. GitHub Issue #53464 참조.

## 해결법
replace `.trim()` with `.replace(/[\n\r\s]+/g, "").trim()` in the token input handlers in:
- `dist/extensions/anthropic/index.js` (line 323)
- `dist/models-C6Rr59E2.js` (line 358)
- `dist/pi-embedded-CzQCqSlH.js` `validateAnthropicSetupToken` (line 10961)

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: gog
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/53464
