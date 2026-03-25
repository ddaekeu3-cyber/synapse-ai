---
layout: solution
title: "Bug: ANTHROPIC_MODEL_ALIASES TDZ crash when contextPruning is configured (3.12)"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/45076
---

# Bug: ANTHROPIC_MODEL_ALIASES TDZ crash when contextPruning is configured (3.12)

## 증상
OpenClaw 3.12 crashes on startup with `ReferenceError: Cannot access 'ANTHROPIC_MODEL_ALIASES' before initialization` when the config contains a custom `contextPruning` section alongside Anthropic model definitions.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Change `const` to `var` for `ANTHROPIC_MODEL_ALIASES` declaration so it hoists (accessed as `undefined` before assignment, safely handled by `??` fallback):

```bash
sed -i '' 's/^const ANTHROPIC_MODEL_ALIASES = {/var ANTHROPIC_MODEL_ALIASES = {/' \
  /opt/homebrew/lib/node_modules/openclaw/dist/auth-profiles-iXW75sRj.js
```

Note: This patch is overwritten on next `npm install -g openclaw`.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/45076
