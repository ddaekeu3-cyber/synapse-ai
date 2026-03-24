---
layout: solution
title: "[Critical][Regression 3.23] kimi-coding completely broken: all tool calls return empty {} args due to missing moonshot-thinking payload compat"
category: openclaw
---

# [Critical][Regression 3.23] kimi-coding completely broken: all tool calls return empty {} args due to missing moonshot-thinking payload compat

## 증상
Regression (worked before, now fails)

에러 메시지:
```js
moonshot: { openAiPayloadNormalizationMode: "moonshot-thinking" },
"kimi-coding": { openAiPayloadNormalizationMode: "moonshot-thinking" },


### Expected behavior

tool call

### Actual behavior

## 원인
원본 이슈에서 확인 필요. GitHub Issue #53591 참조.

## 해결법
Switch to `minimax/MiniMax-M2.7` (same Anthropic API, works correctly).

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: openclaw
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/53591
